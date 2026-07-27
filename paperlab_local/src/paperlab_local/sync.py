"""Private S3-compatible synchronization for PaperLab runtime data."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


MANIFEST_VERSION = 1
STATE_FILE = "data/sync-state.json"


@dataclass(frozen=True)
class SyncSettings:
    root: Path
    bucket: str
    endpoint_url: str | None
    region: str
    prefix: str

    @classmethod
    def from_env(cls, root: Path) -> "SyncSettings":
        bucket = os.environ.get("PAPERLAB_SYNC_BUCKET", "").strip()
        if not bucket:
            raise RuntimeError("PAPERLAB_SYNC_BUCKET is required")
        return cls(
            root=root.resolve(), bucket=bucket,
            endpoint_url=os.environ.get("PAPERLAB_SYNC_ENDPOINT") or None,
            region=os.environ.get("PAPERLAB_SYNC_REGION", "auto"),
            prefix=os.environ.get("PAPERLAB_SYNC_PREFIX", "").strip("/"),
        )

    def key(self, value: str) -> str:
        return f"{self.prefix}/{value}" if self.prefix else value

    @property
    def state_path(self) -> Path:
        return self.root / STATE_FILE


def _client(settings: SyncSettings):
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("sync support is not installed; run: uv pip install -e '.[sync]'") from exc
    return boto3.client("s3", endpoint_url=settings.endpoint_url,
                        region_name=settings.region)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_state(settings: SyncSettings) -> dict:
    try:
        return json.loads(settings.state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def _write_state(settings: SyncSettings, manifest: dict, *,
                 local_database_sha256: str | None = None) -> None:
    settings.state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "revision": manifest["revision"],
        # Pull may rewrite machine-specific paths after verifying the remote
        # object, so the local clean baseline can differ from the object hash.
        "database_sha256": local_database_sha256 or manifest["database"]["sha256"],
        "remote_database_sha256": manifest["database"]["sha256"],
        "files": {relative: item["sha256"] for relative, item in manifest.get("files", {}).items()},
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }
    temp = settings.state_path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, settings.state_path)


def _get_json(client, settings: SyncSettings, key: str) -> dict | None:
    try:
        response = client.get_object(Bucket=settings.bucket, Key=settings.key(key))
    except client.exceptions.NoSuchKey:
        return None
    except Exception as exc:
        response_meta = getattr(exc, "response", {})
        if response_meta.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
            return None
        raise
    return json.loads(response["Body"].read().decode("utf-8"))


def latest_manifest(client, settings: SyncSettings) -> dict | None:
    return _get_json(client, settings, "manifests/latest.json")


def _put_json(client, settings: SyncSettings, key: str, value: dict) -> None:
    body = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    client.put_object(Bucket=settings.bucket, Key=settings.key(key), Body=body,
                      ContentType="application/json")


def create_database_snapshot(db_path: Path, destination: Path) -> None:
    """Create a transactionally consistent SQLite snapshot."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(db_path))
    target = sqlite3.connect(str(destination))
    try:
        source.execute("PRAGMA busy_timeout=10000")
        source.backup(target)
    finally:
        target.close()
        source.close()


def _compress(source: Path, destination: Path) -> None:
    try:
        import zstandard
    except ImportError as exc:
        raise RuntimeError("zstandard is required; install PaperLab with the sync extra") from exc
    compressor = zstandard.ZstdCompressor(level=8, threads=-1)
    with source.open("rb") as src, destination.open("wb") as dst:
        compressor.copy_stream(src, dst)


def _decompress(source: Path, destination: Path) -> None:
    try:
        import zstandard
    except ImportError as exc:
        raise RuntimeError("zstandard is required; install PaperLab with the sync extra") from exc
    decompressor = zstandard.ZstdDecompressor()
    with source.open("rb") as src, destination.open("wb") as dst:
        decompressor.copy_stream(src, dst)


def repair_local_paths(db_path: Path, root: Path) -> None:
    """Rewrite machine-specific artifact paths after restoring a remote snapshot."""
    conn = sqlite3.connect(str(db_path))
    try:
        for paper_id, old_path in conn.execute(
                "SELECT id,pdf_path FROM papers WHERE pdf_path IS NOT NULL").fetchall():
            candidate = root / "pdfs" / Path(old_path).name
            if candidate.is_file():
                conn.execute("UPDATE papers SET pdf_path=?,pdf_status='ok' WHERE id=?",
                             (str(candidate.resolve()), paper_id))
            else:
                conn.execute("UPDATE papers SET pdf_path=NULL,pdf_status=NULL WHERE id=?", (paper_id,))
        for paper_id, old_path in conn.execute(
                "SELECT paper_id,report_path FROM paper_reports").fetchall():
            candidate = root / "reports" / Path(old_path).name
            conn.execute("UPDATE paper_reports SET report_path=?,status=? WHERE paper_id=?",
                         (str(candidate.resolve()), "ok" if candidate.is_file() else "missing", paper_id))
        for topic_id, old_path, old_status in conn.execute(
                "SELECT topic_id,review_path,status FROM topic_reviews WHERE review_path IS NOT NULL").fetchall():
            candidate = root / "reports" / "topics" / Path(old_path).name
            conn.execute("UPDATE topic_reviews SET review_path=?,status=? WHERE topic_id=?",
                         (str(candidate.resolve()), old_status if candidate.is_file() else "missing", topic_id))
        conn.commit()
    finally:
        conn.close()


def _runtime_files(root: Path) -> list[tuple[str, Path, str]]:
    output: list[tuple[str, Path, str]] = []
    pdf_dir = root / "pdfs"
    if pdf_dir.is_dir():
        output.extend((str(path.relative_to(root)), path, "pdf")
                      for path in pdf_dir.rglob("*.pdf") if not path.is_symlink())
    reports = root / "reports"
    if reports.is_dir():
        for path in reports.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = str(path.relative_to(root))
            kind = "review" if relative.startswith("reports/topics/") else "report"
            output.append((relative, path, kind))
    return sorted(output)


def build_file_manifest(root: Path, *, log: Callable[[str], None] = print) -> dict:
    files = {}
    for index, (relative, path, kind) in enumerate(_runtime_files(root), 1):
        digest = _sha256(path)
        suffix = path.suffix.lower()
        cloud_prefix = {"pdf": "pdfs", "report": "artifacts", "review": "reviews"}[kind]
        files[relative] = {
            "key": f"{cloud_prefix}/{digest}{suffix}", "sha256": digest,
            "size": path.stat().st_size, "kind": kind,
        }
        if index % 250 == 0:
            log(f"hashed {index} files")
    return files


def status(cfg, *, log: Callable[[str], None] = print) -> dict:
    settings = SyncSettings.from_env(cfg.root)
    client = _client(settings)
    remote = latest_manifest(client, settings)
    state = _read_state(settings)
    database_changed = None
    if cfg.db_path.is_file() and state.get("database_sha256"):
        with tempfile.TemporaryDirectory(prefix="paperlab_local-status-") as temp_dir:
            snapshot = Path(temp_dir) / "papers.db"
            create_database_snapshot(cfg.db_path, snapshot)
            database_changed = _sha256(snapshot) != state["database_sha256"]
    result = {
        "local_revision": state.get("revision"),
        "remote_revision": remote.get("revision") if remote else None,
        "revision_in_sync": bool(remote and state.get("revision") == remote.get("revision")),
        "local_database_changed": database_changed,
        "remote_files": len(remote.get("files", {})) if remote else 0,
    }
    log(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def push(cfg, *, dry_run: bool = False, force: bool = False,
         log: Callable[[str], None] = print) -> dict:
    settings = SyncSettings.from_env(cfg.root)
    client = _client(settings)
    remote = latest_manifest(client, settings)
    state = _read_state(settings)
    remote_revision = remote.get("revision") if remote else None
    if remote_revision and state.get("revision") != remote_revision and not force:
        raise RuntimeError("remote data changed since the last pull; run `paperlab_local sync pull` first")

    with tempfile.TemporaryDirectory(prefix="paperlab_local-sync-") as temp_dir:
        temp = Path(temp_dir)
        snapshot = temp / "papers.db"
        compressed = temp / "papers.db.zst"
        log("creating consistent SQLite snapshot")
        create_database_snapshot(cfg.db_path, snapshot)
        db_hash = _sha256(snapshot)
        _compress(snapshot, compressed)
        files = build_file_manifest(cfg.root, log=log)
        revision = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        snapshot_conn = sqlite3.connect(str(snapshot))
        try:
            schema_version = snapshot_conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            snapshot_conn.close()
        manifest = {
            "version": MANIFEST_VERSION, "revision": revision,
            "parent_revision": remote_revision,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "schema_version": schema_version,
            "database": {
                "key": f"database/{db_hash}.db.zst", "sha256": db_hash,
                "compressed_size": compressed.stat().st_size,
            },
            "files": files,
        }
        old_files = remote.get("files", {}) if remote else {}
        uploads = [(relative, item) for relative, item in files.items()
                   if old_files.get(relative, {}).get("sha256") != item["sha256"]]
        result = {"revision": revision, "file_uploads": len(uploads),
                  "database_bytes": compressed.stat().st_size, "dry_run": dry_run}
        if dry_run:
            log(json.dumps(result, ensure_ascii=False, indent=2))
            return result
        for index, (relative, item) in enumerate(uploads, 1):
            client.upload_file(str(cfg.root / relative), settings.bucket, settings.key(item["key"]))
            if index % 100 == 0 or index == len(uploads):
                log(f"uploaded {index}/{len(uploads)} data files")
        database_changed = not remote or remote.get("database", {}).get("sha256") != db_hash
        if database_changed:
            client.upload_file(str(compressed), settings.bucket,
                               settings.key(manifest["database"]["key"]))
        _put_json(client, settings, f"manifests/history/{revision}.json", manifest)
        _put_json(client, settings, "manifests/latest.json", manifest)
        _write_state(settings, manifest)
        log(json.dumps(result, ensure_ascii=False, indent=2))
        return result


def pull(cfg, *, include_pdfs: bool = False, force: bool = False,
         log: Callable[[str], None] = print) -> dict:
    settings = SyncSettings.from_env(cfg.root)
    client = _client(settings)
    manifest = latest_manifest(client, settings)
    if not manifest:
        raise RuntimeError("no remote PaperLab snapshot exists")
    state = _read_state(settings)
    if cfg.db_path.exists() and cfg.db_path.stat().st_size > 0 and not state and not force:
        raise RuntimeError("local database is not linked to the remote snapshot; use --force after backing it up")
    local_database_hash = None
    if cfg.db_path.is_file() and state.get("database_sha256"):
        with tempfile.TemporaryDirectory(prefix="paperlab_local-check-") as check_dir:
            local_snapshot = Path(check_dir) / "papers.db"
            create_database_snapshot(cfg.db_path, local_snapshot)
            local_database_hash = _sha256(local_snapshot)
            if local_database_hash != state["database_sha256"] and not force:
                raise RuntimeError("local database changed since the last sync; push it or use --force after backup")
    database_current = (
        cfg.db_path.is_file()
        and state.get("revision") == manifest["revision"]
        and local_database_hash == state.get("database_sha256")
    )
    if database_current:
        log("database revision is already current; checking files")

    downloaded = 0
    with tempfile.TemporaryDirectory(prefix="paperlab_local-pull-") as temp_dir:
        temp = Path(temp_dir)
        compressed = temp / "papers.db.zst"
        restored = temp / "papers.db"
        if not database_current:
            client.download_file(settings.bucket, settings.key(manifest["database"]["key"]), str(compressed))
            _decompress(compressed, restored)
            if _sha256(restored) != manifest["database"]["sha256"]:
                raise RuntimeError("downloaded database checksum mismatch")
            cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
            if cfg.db_path.exists() and cfg.db_path.stat().st_size:
                backup = cfg.db_path.with_suffix(f".db.bak-sync-{int(time.time())}")
                shutil.copy2(cfg.db_path, backup)
                log(f"local database backup: {backup}")
            for suffix in ("-wal", "-shm"):
                Path(str(cfg.db_path) + suffix).unlink(missing_ok=True)
            os.replace(restored, cfg.db_path)

        for relative, item in manifest.get("files", {}).items():
            if item.get("kind") == "pdf" and not include_pdfs:
                continue
            destination = (cfg.root / relative).resolve()
            if cfg.root.resolve() not in destination.parents:
                raise RuntimeError(f"unsafe manifest path: {relative}")
            if destination.is_file() and _sha256(destination) == item["sha256"]:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = destination.with_suffix(destination.suffix + ".sync-tmp")
            client.download_file(settings.bucket, settings.key(item["key"]), str(staging))
            if _sha256(staging) != item["sha256"]:
                staging.unlink(missing_ok=True)
                raise RuntimeError(f"checksum mismatch: {relative}")
            os.replace(staging, destination)
            downloaded += 1
    repair_local_paths(cfg.db_path, cfg.root)
    with tempfile.TemporaryDirectory(prefix="paperlab_local-state-") as state_dir:
        state_snapshot = Path(state_dir) / "papers.db"
        create_database_snapshot(cfg.db_path, state_snapshot)
        local_database_hash = _sha256(state_snapshot)
    _write_state(settings, manifest, local_database_sha256=local_database_hash)
    result = {"revision": manifest["revision"], "downloaded_files": downloaded,
              "pdfs_included": include_pdfs}
    log(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def garbage_collect(cfg, *, apply: bool = False,
                    log: Callable[[str], None] = print) -> dict:
    """Delete unreferenced content objects. Dry-run unless ``apply`` is true."""
    settings = SyncSettings.from_env(cfg.root)
    client = _client(settings)
    manifest = latest_manifest(client, settings)
    if not manifest:
        raise RuntimeError("no remote PaperLab snapshot exists")
    referenced = {settings.key(manifest["database"]["key"])}
    referenced.update(settings.key(item["key"]) for item in manifest.get("files", {}).values())
    candidates = []
    for prefix in ("database/", "pdfs/", "artifacts/", "reviews/"):
        token = None
        while True:
            kwargs = {"Bucket": settings.bucket, "Prefix": settings.key(prefix)}
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)
            candidates.extend(obj["Key"] for obj in page.get("Contents", []) if obj["Key"] not in referenced)
            if not page.get("IsTruncated"):
                break
            token = page["NextContinuationToken"]
    if apply:
        for start in range(0, len(candidates), 1000):
            client.delete_objects(Bucket=settings.bucket,
                                  Delete={"Objects": [{"Key": key} for key in candidates[start:start + 1000]]})
    result = {"unreferenced_objects": len(candidates), "deleted": len(candidates) if apply else 0}
    log(json.dumps(result, ensure_ascii=False, indent=2))
    return result
