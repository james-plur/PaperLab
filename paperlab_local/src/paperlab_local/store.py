"""SQLite source of truth, schema migration, deduplication and stage state."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 11


def normalize_title(title: str) -> str:
    value = (title or "").lower()
    value = re.sub(r"[^\w\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def title_hash(title: str) -> str:
    return hashlib.sha1(normalize_title(title).encode("utf-8")).hexdigest()[:12]


def _clean_arxiv(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(\d{4}\.\d{4,5})(?:v\d+)?", value)
    return match.group(1) if match else value.strip() or None


def compute_id(*, arxiv_id: str | None = None, doi: str | None = None,
               title: str | None = None) -> str:
    if arxiv_id:
        return "arxiv:" + str(_clean_arxiv(arxiv_id))
    if doi:
        return "doi:" + doi.strip().lower()
    if title:
        return "thash:" + title_hash(title)
    raise ValueError("cannot compute id: need arxiv_id, doi, or title")


SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
  id TEXT PRIMARY KEY,
  arxiv_id TEXT,
  doi TEXT,
  norm_title_hash TEXT,
  title TEXT NOT NULL,
  authors TEXT,
  organizations TEXT,
  awards TEXT,
  venue TEXT,
  year INTEGER,
  track TEXT,
  topics TEXT,
  abstract TEXT,
  citation_count INTEGER,
  relevance_score REAL,
  relevance_reason TEXT,
  pdf_path TEXT,
  pdf_status TEXT,
  pdf_url TEXT,
  github_url TEXT,
  target_problem TEXT,
  core_idea TEXT,
  figure_kind TEXT,
  figure_path TEXT,
  figure_mermaid TEXT,
  discovered_at REAL,
  last_updated REAL,
  extraction_version INTEGER,
  published_date TEXT,
  source_url TEXT,
  metadata_status TEXT DEFAULT 'pending',
  authors_json TEXT,
  organizations_json TEXT,
  obsidian_path TEXT,
  obsidian_hash TEXT
);
CREATE TABLE IF NOT EXISTS paper_sources (
  paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  source_id TEXT NOT NULL DEFAULT '',
  landing_url TEXT,
  pdf_url TEXT,
  raw_json TEXT,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  PRIMARY KEY (paper_id, source, source_id)
);
CREATE TABLE IF NOT EXISTS paper_topics (
  paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  topic_id TEXT NOT NULL,
  score REAL NOT NULL,
  relevant INTEGER NOT NULL,
  reason TEXT,
  model TEXT,
  definition_hash TEXT NOT NULL,
  prompt_version INTEGER NOT NULL,
  scored_at REAL NOT NULL,
  PRIMARY KEY (paper_id, topic_id)
);
CREATE TABLE IF NOT EXISTS paper_venues (
  paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  venue TEXT NOT NULL,
  year INTEGER NOT NULL,
  track TEXT,
  source TEXT,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  PRIMARY KEY (paper_id, venue, year)
);
CREATE TABLE IF NOT EXISTS venue_sync (
  venue TEXT NOT NULL,
  year INTEGER NOT NULL,
  status TEXT NOT NULL,
  paper_count INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  checksum TEXT,
  last_attempt REAL NOT NULL,
  last_success REAL,
  PRIMARY KEY (venue, year)
);
CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  started_at REAL NOT NULL,
  finished_at REAL,
  status TEXT NOT NULL,
  details TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(pdf_status);
CREATE INDEX IF NOT EXISTS idx_papers_venue ON papers(venue, year);
CREATE INDEX IF NOT EXISTS idx_papers_arxiv ON papers(arxiv_id);
CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_thash ON papers(norm_title_hash);
CREATE INDEX IF NOT EXISTS idx_topic_relevant ON paper_topics(topic_id, relevant, score);
CREATE INDEX IF NOT EXISTS idx_source_lookup ON paper_sources(source, source_id);
CREATE INDEX IF NOT EXISTS idx_paper_venue_membership ON paper_venues(venue, year, paper_id);
CREATE TABLE IF NOT EXISTS paper_reports (
  paper_id TEXT PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
  report_path TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ok',
  generated_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS collection_topics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL COLLATE NOCASE UNIQUE,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS collection_topic_papers (
  topic_id INTEGER NOT NULL REFERENCES collection_topics(id) ON DELETE CASCADE,
  paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  added_at REAL NOT NULL,
  PRIMARY KEY (topic_id, paper_id)
);
CREATE TABLE IF NOT EXISTS topic_reviews (
  topic_id INTEGER PRIMARY KEY REFERENCES collection_topics(id) ON DELETE CASCADE,
  review_path TEXT,
  status TEXT NOT NULL DEFAULT 'none',
  stage TEXT,
  completed_papers INTEGER NOT NULL DEFAULT 0,
  total_papers INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  snapshot_hash TEXT,
  generated_at REAL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_collection_topic_papers_paper
  ON collection_topic_papers(paper_id, topic_id);
CREATE TABLE IF NOT EXISTS sync_changes (
  change_id TEXT PRIMARY KEY,
  server_sequence INTEGER UNIQUE,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  changed_fields TEXT NOT NULL,
  base_version INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL,
  device_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  applied_at REAL,
  uploaded_at REAL
);
CREATE INDEX IF NOT EXISTS idx_sync_changes_sequence ON sync_changes(server_sequence);
CREATE INDEX IF NOT EXISTS idx_sync_changes_pending ON sync_changes(uploaded_at,created_at);
CREATE TABLE IF NOT EXISTS sync_devices (
  device_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL,
  created_at REAL NOT NULL,
  last_seen REAL,
  revoked_at REAL
);
CREATE TABLE IF NOT EXISTS sync_cursors (
  device_id TEXT PRIMARY KEY,
  last_server_sequence INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_runtime (
  id INTEGER PRIMARY KEY CHECK(id=1),
  last_success REAL,
  last_error TEXT,
  last_attempt REAL
);
CREATE TABLE IF NOT EXISTS sync_conflicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  field TEXT NOT NULL,
  local_value TEXT,
  remote_value TEXT,
  local_version INTEGER NOT NULL,
  remote_version INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at REAL NOT NULL,
  resolved_at REAL
);
CREATE TABLE IF NOT EXISTS stored_objects (
  sha256 TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  content_type TEXT NOT NULL,
  storage_path TEXT NOT NULL,
  created_at REAL NOT NULL,
  deleted_at REAL
);
CREATE TABLE IF NOT EXISTS content_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  content TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  created_at REAL NOT NULL,
  device_id TEXT NOT NULL,
  UNIQUE(entity_type,entity_id,version)
);
CREATE TABLE IF NOT EXISTS paper_activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  action TEXT NOT NULL,
  details TEXT NOT NULL DEFAULT '{}',
  occurred_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paper_activity_time ON paper_activity(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_paper_activity_paper ON paper_activity(paper_id,occurred_at DESC);
CREATE TABLE IF NOT EXISTS paper_summaries (
  paper_id TEXT PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
  content TEXT NOT NULL DEFAULT '',
  abstract_hash TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'none',
  provider TEXT,
  model TEXT,
  error TEXT,
  generated_at REAL,
  updated_at REAL NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  updated_by_device TEXT,
  deleted_at REAL
);
CREATE TABLE IF NOT EXISTS paper_notes (
  paper_id TEXT PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
  note_path TEXT NOT NULL,
  content_hash TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  updated_by_device TEXT,
  sync_error TEXT,
  deleted_at REAL
);
"""

_V2_COLUMNS = {
    "published_date": "TEXT",
    "source_url": "TEXT",
    "metadata_status": "TEXT DEFAULT 'pending'",
    "authors_json": "TEXT",
    "organizations_json": "TEXT",
    "obsidian_path": "TEXT",
    "obsidian_hash": "TEXT",
    "awards": "TEXT",
    "keywords": "TEXT",
    "normalized_orgs": "TEXT",
}

_VERSIONED_COLUMNS = {
    "papers": {"version": "INTEGER NOT NULL DEFAULT 1", "updated_by_device": "TEXT", "deleted_at": "REAL"},
    "collection_topics": {"version": "INTEGER NOT NULL DEFAULT 1", "updated_by_device": "TEXT", "deleted_at": "REAL", "sync_id": "TEXT"},
    "paper_reports": {"version": "INTEGER NOT NULL DEFAULT 1", "updated_by_device": "TEXT", "deleted_at": "REAL"},
    "topic_reviews": {"version": "INTEGER NOT NULL DEFAULT 1", "updated_by_device": "TEXT", "deleted_at": "REAL"},
}


class Store:
    def __init__(self, db_path: str | Path, *, backup_on_migrate: bool = True):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if backup_on_migrate and self._needs_migration():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(self.db_path, self.db_path.with_suffix(f".db.bak-{stamp}"))
        previous_version = 0
        if self.db_path.exists() and self.db_path.stat().st_size:
            probe = sqlite3.connect(self.db_path)
            try:
                previous_version = int(probe.execute("PRAGMA user_version").fetchone()[0])
            finally:
                probe.close()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._ensure_columns()
        self._migrate_legacy_rows()
        if previous_version < 11:
            self._migrate_v11_remove_reports()
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()

    def _migrate_v11_remove_reports(self) -> None:
        """Remove obsolete machine-generated per-paper reports, never topic reviews."""
        if not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_reports'"
        ).fetchone():
            return
        for row in self.conn.execute("SELECT report_path FROM paper_reports WHERE report_path IS NOT NULL"):
            try:
                Path(row[0]).unlink(missing_ok=True)
            except OSError:
                pass
        self.conn.execute("DELETE FROM paper_reports")
        self.conn.execute("DELETE FROM content_versions WHERE entity_type='report'")
        self.conn.execute("DELETE FROM sync_changes WHERE entity_type='report'")
        self.conn.execute("DELETE FROM sync_conflicts WHERE entity_type='report'")

    def _needs_migration(self) -> bool:
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return False
        conn = sqlite3.connect(self.db_path)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return "papers" in tables and version < SCHEMA_VERSION
        finally:
            conn.close()

    def _ensure_columns(self) -> None:
        existing = {r[1] for r in self.conn.execute("PRAGMA table_info(papers)")}
        for name, decl in _V2_COLUMNS.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE papers ADD COLUMN {name} {decl}")
        for table, columns in _VERSIONED_COLUMNS.items():
            existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        import uuid
        for row in self.conn.execute("SELECT id FROM collection_topics WHERE sync_id IS NULL OR sync_id='' ").fetchall():
            self.conn.execute("UPDATE collection_topics SET sync_id=? WHERE id=?", (str(uuid.uuid4()), row[0]))
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_collection_topics_sync_id ON collection_topics(sync_id)")

    def record_change(self, entity_type: str, entity_id: str, operation: str,
                      fields: dict, *, base_version: int = 0, version: int = 1,
                      device_id: str | None = None, server_sequence: int | None = None,
                      change_id: str | None = None, uploaded: bool = False,
                      created_at: float | None = None) -> str:
        """Append an idempotent sync change in the caller's transaction."""
        import uuid
        cid = change_id or str(uuid.uuid4())
        device = device_id or os.environ.get("PAPERLAB_DEVICE_ID", "local")
        now = time.time()
        if server_sequence is None and os.environ.get("PAPERLAB_SYNC_SERVER", "0").lower() in {"1", "true", "yes"}:
            server_sequence = self.next_server_sequence()
            uploaded = True
        self.conn.execute(
            """INSERT OR IGNORE INTO sync_changes
               (change_id,server_sequence,entity_type,entity_id,operation,changed_fields,
                base_version,version,device_id,created_at,applied_at,uploaded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, server_sequence, entity_type, str(entity_id), operation,
             json.dumps(fields, ensure_ascii=False, sort_keys=True), int(base_version),
             int(version), device, float(created_at) if created_at is not None else now,
             now, now if uploaded else None),
        )
        return cid

    def record_activity(self, paper_id: str, action: str, details: dict | None = None,
                        *, dedupe_seconds: int = 0) -> None:
        now = time.time()
        if dedupe_seconds:
            recent = self.conn.execute(
                "SELECT occurred_at FROM paper_activity WHERE paper_id=? AND action=? ORDER BY occurred_at DESC LIMIT 1",
                (paper_id, action),
            ).fetchone()
            if recent and now - float(recent[0]) < dedupe_seconds:
                return
        self.conn.execute(
            "INSERT INTO paper_activity(paper_id,action,details,occurred_at) VALUES (?,?,?,?)",
            (paper_id, action, json.dumps(details or {}, ensure_ascii=False), now),
        )
        self.conn.execute("DELETE FROM paper_activity WHERE occurred_at<?", (now - 180 * 86400,))
        self.conn.execute(
            "DELETE FROM paper_activity WHERE id NOT IN (SELECT id FROM paper_activity ORDER BY occurred_at DESC LIMIT 1000)"
        )
        self.conn.commit()

    def recent_activity(self, *, limit: int = 50, offset: int = 0,
                        actions: list[str] | None = None, unique_papers: bool = False) -> list[dict]:
        where = ""
        params: list[Any] = []
        if actions:
            where = "WHERE a.action IN (" + ",".join("?" for _ in actions) + ")"
            params.extend(actions)
        if unique_papers:
            sql = f"""SELECT * FROM (
                SELECT a.*,p.title,p.authors,p.venue,p.year,p.pdf_status,
                       ROW_NUMBER() OVER (PARTITION BY a.paper_id ORDER BY a.occurred_at DESC) AS rn
                FROM paper_activity a JOIN papers p ON p.id=a.paper_id {where}
              ) WHERE rn=1 ORDER BY occurred_at DESC LIMIT ? OFFSET ?"""
        else:
            sql = f"""SELECT a.*,p.title,p.authors,p.venue,p.year,p.pdf_status
                FROM paper_activity a JOIN papers p ON p.id=a.paper_id {where}
                ORDER BY a.occurred_at DESC LIMIT ? OFFSET ?"""
        params.extend([max(1, min(200, limit)), max(0, offset)])
        rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item.pop("rn", None)
            try:
                item["details"] = json.loads(item.get("details") or "{}")
            except ValueError:
                item["details"] = {}
            result.append(item)
        return result

    def next_server_sequence(self) -> int:
        return int(self.conn.execute(
            "SELECT COALESCE(max(server_sequence),0)+1 FROM sync_changes"
        ).fetchone()[0])

    def pending_changes(self, limit: int = 500) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sync_changes WHERE uploaded_at IS NULL ORDER BY created_at,change_id LIMIT ?",
            (limit,),
        ).fetchall()

    def changes_after(self, sequence: int, limit: int = 500) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM sync_changes WHERE server_sequence>? AND server_sequence IS NOT NULL
               ORDER BY server_sequence LIMIT ?""", (sequence, limit)
        ).fetchall()

    def run_sync_maintenance(self, *, now: float | None = None) -> dict:
        now = now or time.time()
        min_cursor = self.conn.execute(
            "SELECT min(last_server_sequence) FROM sync_cursors"
        ).fetchone()[0]
        removed_changes = 0
        if min_cursor is not None:
            result = self.conn.execute(
                "DELETE FROM sync_changes WHERE server_sequence<=? AND created_at<?",
                (min_cursor, now - 90 * 86400),
            )
            removed_changes = result.rowcount
        removed_topics = self.conn.execute(
            "DELETE FROM collection_topics WHERE deleted_at IS NOT NULL AND deleted_at<?",
            (now - 30 * 86400,),
        ).rowcount
        self.conn.commit()
        return {"changes": removed_changes, "topics": removed_topics}

    def register_device(self, device_id: str, name: str, token_hash: str) -> None:
        now = time.time()
        self.conn.execute(
            """INSERT INTO sync_devices(device_id,name,token_hash,created_at)
               VALUES (?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET
               name=excluded.name,token_hash=excluded.token_hash,revoked_at=NULL""",
            (device_id, name, token_hash, now),
        )
        self.conn.commit()

    def register_object(self, path: str | Path, content_type: str) -> str:
        path = Path(path).resolve()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        value = digest.hexdigest()
        self.conn.execute(
            """INSERT INTO stored_objects(sha256,size,content_type,storage_path,created_at)
               VALUES (?,?,?,?,?) ON CONFLICT(sha256) DO UPDATE SET
               storage_path=excluded.storage_path,size=excluded.size,deleted_at=NULL""",
            (value, path.stat().st_size, content_type, str(path), time.time()),
        )
        return value

    def archive_content(self, entity_type: str, entity_id: str, version: int,
                        path: str | Path) -> None:
        path = Path(path)
        if not path.is_file():
            return
        content = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(content.encode()).hexdigest()
        self.conn.execute(
            """INSERT OR IGNORE INTO content_versions
               (entity_type,entity_id,version,content,content_hash,created_at,device_id)
               VALUES (?,?,?,?,?,?,?)""",
            (entity_type, str(entity_id), int(version), content, digest, time.time(),
             os.environ.get("PAPERLAB_DEVICE_ID", "local")),
        )

    def markdown_objects(self, content: str, markdown_path: str | Path) -> list[dict]:
        base = Path(markdown_path).resolve().parent
        objects = []
        for relative in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", content):
            if re.match(r"^[a-z]+://|^/", relative, re.I) or ".." in Path(relative).parts:
                continue
            path = (base / relative).resolve()
            if path.is_file() and path.is_relative_to(base):
                content_type = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                                ".gif": "image/gif", ".svg": "image/svg+xml"}.get(
                                    path.suffix.lower(), "application/octet-stream")
                objects.append({"sha256": self.register_object(path, content_type), "path": relative,
                                "content_type": content_type})
        return objects

    def device_for_token(self, token_hash: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sync_devices WHERE token_hash=? AND revoked_at IS NULL", (token_hash,)
        ).fetchone()

    def update_topic_name(self, topic_id: int, name: str, *, base_version: int | None = None) -> sqlite3.Row:
        clean = str(name or "").strip()
        if not clean:
            raise ValueError("topic name cannot be empty")
        row = self.get_collection_topic(topic_id)
        if row is None:
            raise KeyError("topic not found")
        current = int(row["version"] or 1)
        if base_version is not None and int(base_version) != current:
            raise RuntimeError("version conflict")
        now = time.time()
        self.conn.execute(
            "UPDATE collection_topics SET name=?,updated_at=?,version=?,updated_by_device=? WHERE id=?",
            (clean, now, current + 1, os.environ.get("PAPERLAB_DEVICE_ID", "local"), topic_id),
        )
        self.record_change("topic", row["sync_id"], "update", {"name": clean},
                           base_version=current, version=current + 1)
        self.conn.commit()
        return self.get_collection_topic(topic_id)

    def _migrate_legacy_rows(self) -> None:
        """Move the v1 import bucket out of the relevance-topic field."""
        now = time.time()
        self.conn.execute(
            """INSERT OR IGNORE INTO paper_sources
               (paper_id,source,source_id,landing_url,pdf_url,raw_json,first_seen,last_seen)
               SELECT id,'legacy-conf-import',id,source_url,pdf_url,NULL,
                      COALESCE(discovered_at,?),COALESCE(last_updated,?)
               FROM papers WHERE ','||topics||',' LIKE '%,conf2026,%'""",
            (now, now),
        )
        self.conn.execute(
            """UPDATE papers SET topics=trim(replace(','||topics||',', ',conf2026,', ','), ',')
               WHERE ','||topics||',' LIKE '%,conf2026,%'"""
        )
        self.conn.execute(
            """INSERT OR IGNORE INTO paper_venues
               (paper_id,venue,year,track,source,first_seen,last_seen)
               SELECT id,venue,year,track,'papers',COALESCE(discovered_at,?),COALESCE(last_updated,?)
               FROM papers WHERE venue IS NOT NULL AND venue!='' AND year IS NOT NULL""",
            (now, now),
        )
        self.conn.execute(
            """INSERT OR IGNORE INTO paper_venues
               (paper_id,venue,year,track,source,first_seen,last_seen)
               SELECT paper_id,upper(json_extract(raw_json,'$.venue')),
                      cast(json_extract(raw_json,'$.year') AS INTEGER),
                      json_extract(raw_json,'$.track'),source,first_seen,last_seen
               FROM paper_sources
               WHERE json_valid(raw_json)
                 AND json_extract(raw_json,'$.venue') IS NOT NULL
                 AND json_extract(raw_json,'$.year') IS NOT NULL"""
        )


    def get_keywords(self, paper_id: str) -> list[str]:
        row = self.conn.execute(
            "SELECT keywords FROM papers WHERE id=?", (paper_id,)).fetchone()
        if not row or not row["keywords"]:
            return []
        return [k.strip() for k in row["keywords"].split(",") if k.strip()]

    def _record_paper_update(self, paper_id: str, fields: dict, row=None) -> None:
        row = row or self.get(paper_id)
        if row is None or not fields:
            return
        current = int(row["version"] or 1)
        version = current + 1
        self.conn.execute("UPDATE papers SET version=?,updated_by_device=? WHERE id=?",
                          (version, os.environ.get("PAPERLAB_DEVICE_ID", "local"), paper_id))
        self.record_change("paper", paper_id, "update", fields,
                           base_version=current, version=version)

    def set_keywords(self, paper_id: str, keywords: list[str]) -> None:
        kw = ",".join(sorted(set(k.strip() for k in keywords if k.strip())))
        row = self.get(paper_id)
        self.conn.execute(
            "UPDATE papers SET keywords=?, last_updated=? WHERE id=?",
            (kw, time.time(), paper_id))
        self._record_paper_update(paper_id, {"keywords": kw}, row)
        self.conn.commit()

    def add_keywords(self, paper_id: str, keywords: list[str]) -> None:
        existing = set(self.get_keywords(paper_id))
        existing.update(k.strip() for k in keywords if k.strip())
        self.set_keywords(paper_id, list(existing))

    def papers_without_keywords(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT id, title, abstract FROM papers "
               "WHERE (keywords IS NULL OR keywords='') AND abstract IS NOT NULL AND trim(abstract)!='' "
               "ORDER BY year DESC, discovered_at DESC")
        if limit:
            sql += " LIMIT ?"
            return self.conn.execute(sql, (limit,)).fetchall()
        return self.conn.execute(sql).fetchall()

    def search_by_keywords(self, query: str, limit: int = 50) -> list[sqlite3.Row]:
        """Full-text search on title, abstract, and keywords."""
        token = f"%{query.lower()}%"
        return self.conn.execute(
            """SELECT * FROM papers
               WHERE lower(title) LIKE ? OR lower(abstract) LIKE ? OR lower(keywords) LIKE ?
               ORDER BY year DESC, title LIMIT ?""",
            (token, token, token, limit)).fetchall()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @staticmethod
    def _add_topic(existing: str | None, topic: str) -> str:
        values = {x for x in (existing or "").split(",") if x}
        values.add(topic)
        return ",".join(sorted(values))

    def _find_existing(self, arxiv_id: str | None, doi: str | None,
                       normalized_hash: str) -> sqlite3.Row | None:
        if arxiv_id:
            row = self.conn.execute("SELECT * FROM papers WHERE arxiv_id=?", (arxiv_id,)).fetchone()
            if row:
                return row
        if doi:
            # DOI values are normalized to lowercase before insertion.  An
            # exact comparison lets SQLite use idx_papers_doi; wrapping the
            # column in lower() forced a full 90k-row scan for every paper.
            row = self.conn.execute("SELECT * FROM papers WHERE doi=?", (doi.lower(),)).fetchone()
            if row:
                return row
        return self.conn.execute(
            "SELECT * FROM papers WHERE norm_title_hash=?", (normalized_hash,)
        ).fetchone()

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not value:
            return []
        if isinstance(value, list):
            return [str(x).strip() for x in value if str(x).strip()]
        return [x.strip() for x in re.split(r"\s*;\s*|\s*,\s*", str(value)) if x.strip()]

    def upsert_candidate(self, candidate: dict, topic_name: str | None = None,
                         *, commit: bool = True, track_change: bool = True) -> str:
        title = str(candidate.get("title") or "").strip()
        if not title:
            raise ValueError("candidate missing title")
        arxiv_id = _clean_arxiv(candidate.get("arxiv_id"))
        doi = str(candidate.get("doi") or "").strip().lower() or None
        normalized_hash = title_hash(title)
        now = time.time()
        row = self._find_existing(arxiv_id, doi, normalized_hash)
        created = row is None
        if created:
            paper_id = compute_id(arxiv_id=arxiv_id, doi=doi, title=title)
            authors = candidate.get("authors")
            organizations = candidate.get("organizations")
            self.conn.execute(
                """INSERT INTO papers
                (id, arxiv_id, doi, norm_title_hash, title, authors, organizations, awards,
                 venue, year, track, topics, abstract, keywords, citation_count, pdf_status,
                 pdf_url, discovered_at, last_updated, published_date, source_url,
                 metadata_status, authors_json, organizations_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (paper_id, arxiv_id, doi, normalized_hash, title, authors, organizations,
                 candidate.get("awards"), candidate.get("venue"), candidate.get("year"), candidate.get("track"),
                 topic_name or "", candidate.get("abstract"), candidate.get("keywords"), candidate.get("citation_count"),
                 None, candidate.get("pdf_url"), now, now, candidate.get("published_date"),
                 candidate.get("source_url") or candidate.get("url"),
                 "complete" if candidate.get("abstract") else "pending",
                 json.dumps(self._string_list(authors), ensure_ascii=False),
                 json.dumps(self._string_list(organizations), ensure_ascii=False)),
            )
        else:
            paper_id = row["id"]
            self._merge_into(paper_id, row, candidate, topic_name, arxiv_id, doi, now)
        source = str(candidate.get("source") or candidate.get("venue") or "catalog").lower()
        source_id = str(candidate.get("source_id") or candidate.get("paper_id") or "")
        self.upsert_source(
            paper_id, source=source, source_id=source_id,
            landing_url=candidate.get("source_url") or candidate.get("url"),
            pdf_url=candidate.get("pdf_url"), raw=candidate,
        )
        if candidate.get("venue") and candidate.get("year"):
            self.upsert_venue(
                paper_id, venue=str(candidate["venue"]).upper(), year=int(candidate["year"]),
                track=candidate.get("track"), source=source,
            )
            if track_change:
                venue_fields = {"paper_id": paper_id, "venue": str(candidate["venue"]).upper(),
                                "year": int(candidate["year"]), "track": candidate.get("track"),
                                "source": source}
                self.record_change("paper_venue", f"{paper_id}:{venue_fields['venue']}:{venue_fields['year']}",
                                   "update", venue_fields)
        if track_change:
            synced = ("title", "authors", "organizations", "awards", "venue", "year", "track",
                      "abstract", "citation_count", "pdf_url", "published_date", "source_url",
                      "arxiv_id", "doi", "keywords")
            after = self.get(paper_id)
            changed = {key: after[key] for key in synced
                       if after and (created or (key in row.keys() and after[key] != row[key]))}
            if changed:
                base = 0 if created else int(row["version"] or 1)
                version = 1 if created else base + 1
                if not created:
                    self.conn.execute("UPDATE papers SET version=?,updated_by_device=? WHERE id=?",
                                      (version, os.environ.get("PAPERLAB_DEVICE_ID", "local"), paper_id))
                self.record_change("paper", paper_id, "create" if created else "update", changed,
                                   base_version=base, version=version)
        if commit:
            self.conn.commit()
        return paper_id

    def upsert_venue(self, paper_id: str, *, venue: str, year: int,
                     track: str | None = None, source: str = "catalog") -> None:
        now = time.time()
        self.conn.execute(
            """INSERT INTO paper_venues
               (paper_id,venue,year,track,source,first_seen,last_seen)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(paper_id,venue,year) DO UPDATE SET
                 track=COALESCE(excluded.track,paper_venues.track),
                 source=excluded.source,last_seen=excluded.last_seen""",
            (paper_id, venue.upper(), int(year), track, source, now, now),
        )

    def _merge_into(self, paper_id: str, row: sqlite3.Row, candidate: dict,
                    topic_name: str | None, arxiv_id: str | None, doi: str | None,
                    now: float) -> None:
        updates: dict[str, Any] = {"last_updated": now}
        if topic_name:
            updates["topics"] = self._add_topic(row["topics"], topic_name)
        if arxiv_id and not row["arxiv_id"]:
            updates["arxiv_id"] = arxiv_id
        if doi and not row["doi"]:
            updates["doi"] = doi
        venue = candidate.get("venue")
        candidate_source = str(candidate.get("source") or "").lower()
        # The legacy conf2026 import assigned year=2026 to every row.  The
        # first later catalogue record is authoritative for venue/year and
        # must be allowed to correct that provisional label.  Once any
        # non-legacy catalogue source exists, do not keep flipping identity.
        legacy_only = self.conn.execute(
            """SELECT EXISTS(SELECT 1 FROM paper_sources WHERE paper_id=? AND source='legacy-conf-import')
                      AND NOT EXISTS(SELECT 1 FROM paper_sources WHERE paper_id=?
                                     AND source!='legacy-conf-import'
                                     AND json_valid(raw_json)
                                     AND json_extract(raw_json,'$.venue') IS NOT NULL
                                     AND json_extract(raw_json,'$.year') IS NOT NULL)""",
            (paper_id, paper_id),
        ).fetchone()[0]
        if (legacy_only and candidate_source != "legacy-conf-import"):
            if venue:
                updates["venue"] = venue
            if candidate.get("year"):
                updates["year"] = candidate.get("year")
        if venue and (not row["venue"] or (row["venue"] == "arXiv" and venue != "arXiv")):
            updates["venue"] = venue
        for key in ("authors", "organizations", "awards", "year", "track", "abstract", "keywords",
                    "citation_count", "pdf_url", "published_date", "source_url"):
            value = candidate.get(key)
            if key == "source_url" and not value:
                value = candidate.get("url")
            if value not in (None, "") and row[key] in (None, ""):
                updates[key] = value
        if updates.get("authors"):
            updates["authors_json"] = json.dumps(self._string_list(updates["authors"]), ensure_ascii=False)
        if updates.get("organizations"):
            updates["organizations_json"] = json.dumps(self._string_list(updates["organizations"]), ensure_ascii=False)
        if updates.get("abstract") or row["abstract"]:
            updates["metadata_status"] = "complete"
        self.conn.execute(
            "UPDATE papers SET " + ", ".join(f"{key}=?" for key in updates) + " WHERE id=?",
            (*updates.values(), paper_id),
        )

    def upsert_source(self, paper_id: str, *, source: str, source_id: str = "",
                      landing_url: str | None = None, pdf_url: str | None = None,
                      raw: dict | None = None) -> None:
        now = time.time()
        self.conn.execute(
            """INSERT INTO paper_sources
               (paper_id,source,source_id,landing_url,pdf_url,raw_json,first_seen,last_seen)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(paper_id,source,source_id) DO UPDATE SET
                 landing_url=COALESCE(excluded.landing_url,paper_sources.landing_url),
                 pdf_url=COALESCE(excluded.pdf_url,paper_sources.pdf_url),
                 raw_json=COALESCE(excluded.raw_json,paper_sources.raw_json),
                 last_seen=excluded.last_seen""",
            (paper_id, source, source_id, landing_url, pdf_url,
             json.dumps(raw, ensure_ascii=False, default=str) if raw else None, now, now),
        )

    def set_enrichment(self, paper_id: str, fields: dict, *, commit: bool = True) -> None:
        row = self.get(paper_id)
        if row is None:
            return
        allowed = ("arxiv_id", "doi", "abstract", "citation_count", "github_url",
                   "pdf_url", "organizations", "authors", "venue", "year",
                   "published_date", "source_url", "metadata_status", "awards")
        updates: dict[str, Any] = {}
        for key in allowed:
            if fields.get(key) not in (None, ""):
                value = _clean_arxiv(fields[key]) if key == "arxiv_id" else fields[key]
                if row[key] in (None, "") or key in ("citation_count", "metadata_status"):
                    updates[key] = value
        if updates.get("authors"):
            updates["authors_json"] = json.dumps(self._string_list(updates["authors"]), ensure_ascii=False)
        if updates.get("organizations"):
            updates["organizations_json"] = json.dumps(self._string_list(updates["organizations"]), ensure_ascii=False)
        if fields.get("topic"):
            updates["topics"] = self._add_topic(row["topics"], fields["topic"])
        if not updates:
            return
        updates["last_updated"] = time.time()
        self.conn.execute(
            "UPDATE papers SET " + ", ".join(f"{key}=?" for key in updates) + " WHERE id=?",
            (*updates.values(), paper_id),
        )
        sync_updates = {k: v for k, v in updates.items() if k != "last_updated"}
        self._record_paper_update(paper_id, sync_updates, row)
        if commit:
            self.conn.commit()

    def pending_metadata(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = """SELECT * FROM papers
                 WHERE (metadata_status!='complete'
                    OR abstract IS NULL OR trim(abstract)=''
                    OR organizations IS NULL OR trim(organizations)='')
                 AND NOT EXISTS (
                   SELECT 1 FROM paper_sources ps
                   WHERE ps.paper_id=papers.id AND ps.source='paper-search-mcp-attempt')
                 ORDER BY year DESC, discovered_at"""
        if limit is not None:
            sql += " LIMIT ?"
            return self.conn.execute(sql, (limit,)).fetchall()
        return self.conn.execute(sql).fetchall()

    def set_topic_score(self, paper_id: str, topic_id: str, score: float, reason: str,
                        *, threshold: float, model: str, definition_hash: str,
                        prompt_version: int) -> None:
        row = self.get(paper_id)
        score = max(0.0, min(1.0, float(score)))
        self.conn.execute(
            """INSERT INTO paper_topics
               (paper_id,topic_id,score,relevant,reason,model,definition_hash,prompt_version,scored_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(paper_id,topic_id) DO UPDATE SET
                 score=excluded.score,relevant=excluded.relevant,reason=excluded.reason,
                 model=excluded.model,definition_hash=excluded.definition_hash,
                 prompt_version=excluded.prompt_version,scored_at=excluded.scored_at""",
            (paper_id, topic_id, score, int(score >= threshold), reason[:500], model,
             definition_hash, prompt_version, time.time()),
        )
        # Maintain legacy columns for old index/extract code when there is only
        # one visible score, but v2 logic never reads these for topic decisions.
        self.conn.execute(
            "UPDATE papers SET relevance_score=?, relevance_reason=?, last_updated=? WHERE id=?",
            (score, reason[:500], time.time(), paper_id),
        )
        self._record_paper_update(paper_id, {"relevance_score": score,
                                             "relevance_reason": reason[:500]}, row)
        self.record_change("paper_topic_score", f"{paper_id}:{topic_id}", "update", {
            "paper_id": paper_id, "topic_id": topic_id, "score": score,
            "relevant": int(score >= threshold), "reason": reason[:500], "model": model,
            "definition_hash": definition_hash, "prompt_version": prompt_version,
        })
        self.conn.commit()

    def set_relevance(self, paper_id: str, score: float, reason: str = "") -> None:
        """Backward-compatible v1 relevance setter."""
        row = self.get(paper_id)
        self.conn.execute(
            "UPDATE papers SET relevance_score=?, relevance_reason=?, last_updated=? WHERE id=?",
            (score, reason, time.time(), paper_id),
        )
        self._record_paper_update(paper_id, {"relevance_score": score, "relevance_reason": reason}, row)
        self.conn.commit()

    def pending_topic(self, topic_id: str, definition_hash: str, prompt_version: int,
                      *, force: bool = False) -> list[sqlite3.Row]:
        if force:
            return self.all()
        return self.conn.execute(
            """SELECT p.* FROM papers p LEFT JOIN paper_topics pt
               ON pt.paper_id=p.id AND pt.topic_id=?
               WHERE pt.paper_id IS NULL OR pt.definition_hash!=? OR pt.prompt_version!=?
               ORDER BY p.year DESC,p.title""",
            (topic_id, definition_hash, prompt_version),
        ).fetchall()

    def topic_scores(self, paper_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM paper_topics WHERE paper_id=? ORDER BY topic_id", (paper_id,)
        ).fetchall()

    def topic_papers_v2(self, topic_id: str, *, min_score: float | None = None,
                        relevant_only: bool = True) -> list[sqlite3.Row]:
        clauses = ["pt.topic_id=?"]
        params: list[Any] = [topic_id]
        if relevant_only:
            clauses.append("pt.relevant=1")
        if min_score is not None:
            clauses.append("pt.score>=?")
            params.append(min_score)
        return self.conn.execute(
            """SELECT p.*,pt.score AS topic_score,pt.relevant AS topic_relevant,
                      pt.reason AS topic_reason
               FROM papers p JOIN paper_topics pt ON pt.paper_id=p.id
               WHERE """ + " AND ".join(clauses) +
            " ORDER BY pt.score DESC,p.year DESC,p.title",
            params,
        ).fetchall()

    def has_topic_scores(self, topic_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM paper_topics WHERE topic_id=? LIMIT 1", (topic_id,)
        ).fetchone() is not None

    def set_pdf(self, paper_id: str, *, path: str | None, status: str,
                pdf_url: str | None = None) -> None:
        row = self.get(paper_id)
        current = int(row["version"] or 1) if row else 1
        digest = self.register_object(path, "application/pdf") if path and Path(path).is_file() else None
        self.conn.execute(
            """UPDATE papers SET pdf_path=?,pdf_status=?,pdf_url=COALESCE(?,pdf_url),
               last_updated=?,version=?,updated_by_device=? WHERE id=?""",
            (path, status, pdf_url, time.time(), current + 1,
             os.environ.get("PAPERLAB_DEVICE_ID", "local"), paper_id),
        )
        self.record_change("paper", paper_id, "update",
                           {"pdf_status": status, "pdf_url": pdf_url, "object_sha256": digest},
                           base_version=current, version=current + 1)
        self.conn.commit()

    def set_extraction(self, paper_id: str, *, target_problem: str | None,
                       core_idea: str | None, organizations: str | None,
                       github_url: str | None, figure_kind: str | None,
                       figure_path: str | None = None,
                       figure_mermaid: str | None = None, version: int = 1) -> None:
        row = self.get(paper_id)
        self.conn.execute(
            """UPDATE papers SET target_problem=?,core_idea=?,organizations=?,github_url=?,
               figure_kind=?,figure_path=?,figure_mermaid=?,extraction_version=?,last_updated=?
               WHERE id=?""",
            (target_problem, core_idea, organizations, github_url, figure_kind,
             figure_path, figure_mermaid, version, time.time(), paper_id),
        )
        self._record_paper_update(paper_id, {
            "target_problem": target_problem, "core_idea": core_idea,
            "organizations": organizations, "github_url": github_url,
            "figure_kind": figure_kind, "figure_mermaid": figure_mermaid,
            "extraction_version": version,
        }, row)
        self.conn.commit()

    def set_obsidian(self, paper_id: str, path: str, content_hash: str,
                     *, commit: bool = True) -> None:
        self.conn.execute(
            "UPDATE papers SET obsidian_path=?,obsidian_hash=? WHERE id=?",
            (path, content_hash, paper_id),
        )
        if commit:
            self.conn.commit()

    def get(self, paper_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()

    def all(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM papers ORDER BY year DESC,relevance_score DESC,title"
        ).fetchall()

    def topic_papers(self, topic: str) -> list[sqlite3.Row]:
        # Prefer v2 scores; fall back to legacy comma-separated collection topics.
        if self.has_topic_scores(topic):
            return self.topic_papers_v2(topic, relevant_only=False)
        return self.conn.execute(
            "SELECT * FROM papers WHERE ','||topics||',' LIKE ? ORDER BY year DESC,title",
            (f"%,{topic},%",),
        ).fetchall()

    def pending(self, topic: str, stage: str) -> list[sqlite3.Row]:
        rows = self.topic_papers(topic)
        output = []
        for row in rows:
            if stage == "relevance" and row["relevance_score"] is None:
                output.append(row)
            elif stage == "download" and row["relevance_score"] is not None and row["pdf_status"] is None:
                output.append(row)
            elif stage == "extract" and row["pdf_status"] == "ok" and row["extraction_version"] is None:
                output.append(row)
        return output

    def record_venue_sync(self, venue: str, year: int, *, status: str,
                          count: int = 0, error: str = "", checksum: str = "") -> None:
        now = time.time()
        self.conn.execute(
            """INSERT INTO venue_sync
               (venue,year,status,paper_count,error,checksum,last_attempt,last_success)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(venue,year) DO UPDATE SET status=excluded.status,
                 paper_count=excluded.paper_count,error=excluded.error,checksum=excluded.checksum,
                 last_attempt=excluded.last_attempt,
                 last_success=COALESCE(excluded.last_success,venue_sync.last_success)""",
            (venue, year, status, count, error[:1000], checksum, now,
             now if status == "ok" else None),
        )
        self.conn.commit()

    def stats(self, topic: str | None = None) -> dict:
        rows = self.topic_papers_v2(topic, relevant_only=False) if topic and self.has_topic_scores(topic) else (
            self.topic_papers(topic) if topic else self.all())
        return {
            "total": len(rows),
            "scored": sum(1 for row in rows if ("topic_score" in row.keys() or row["relevance_score"] is not None)),
            "relevant": (sum(1 for row in rows if row["topic_relevant"] == 1)
                         if rows and "topic_relevant" in rows[0].keys() else None),
            "downloaded": sum(1 for row in rows if row["pdf_status"] == "ok"),
            "extracted": sum(1 for row in rows if row["extraction_version"] is not None),
        }

    def get_report(self, paper_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM paper_reports WHERE paper_id=?", (paper_id,)).fetchone()

    def get_summary(self, paper_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM paper_summaries WHERE paper_id=? AND deleted_at IS NULL", (paper_id,)
        ).fetchone()

    def set_summary(self, paper_id: str, *, content: str, abstract_hash: str,
                    status: str = "ok", provider: str | None = None,
                    model: str | None = None, error: str | None = None,
                    track_change: bool = True) -> sqlite3.Row:
        now = time.time()
        existing = self.get_summary(paper_id)
        current = int(existing["version"] or 1) if existing else 0
        version = current + 1
        device = os.environ.get("PAPERLAB_DEVICE_ID", "local")
        self.conn.execute(
            """INSERT INTO paper_summaries
               (paper_id,content,abstract_hash,status,provider,model,error,generated_at,updated_at,version,updated_by_device)
               VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET
               content=excluded.content,abstract_hash=excluded.abstract_hash,status=excluded.status,
               provider=excluded.provider,model=excluded.model,error=excluded.error,
               generated_at=excluded.generated_at,updated_at=excluded.updated_at,
               version=excluded.version,updated_by_device=excluded.updated_by_device,deleted_at=NULL""",
            (paper_id, content, abstract_hash, status, provider, model, error,
             now if status == "ok" else None, now, version, device),
        )
        if track_change:
            self.record_change("paper_summary", paper_id, "update", {
                "content": content, "abstract_hash": abstract_hash, "status": status,
                "provider": provider, "model": model, "generated_at": now if status == "ok" else None,
            }, base_version=current, version=version)
        self.conn.commit()
        return self.get_summary(paper_id)

    def get_note(self, paper_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM paper_notes WHERE paper_id=? AND deleted_at IS NULL", (paper_id,)
        ).fetchone()

    def save_note_content(self, pdf_root: Path, paper_id: str, content: str,
                          *, base_version: int | None = None) -> sqlite3.Row:
        from .reader import _note_path, save_note
        if len(content.encode("utf-8")) > 5 * 1024 * 1024:
            raise ValueError("note exceeds 5 MB sync limit")
        self.conn.execute("BEGIN IMMEDIATE")
        path = _note_path(pdf_root, paper_id)
        previous = path.read_bytes() if path.is_file() else None
        wrote = False
        try:
            existing = self.get_note(paper_id)
            current = int(existing["version"]) if existing else 0
            if base_version is not None and int(base_version) != current:
                raise RuntimeError("version conflict")
            save_note(pdf_root, paper_id, content)
            wrote = True
            return self.set_note(paper_id, note_path=str(path), content=content,
                                 base_version=current)
        except Exception:
            if wrote:
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    temp = path.with_suffix(".md.restore")
                    temp.write_bytes(previous)
                    os.replace(temp, path)
            self.conn.rollback()
            raise

    def set_note(self, paper_id: str, *, note_path: str, content: str,
                 base_version: int | None = None, track_change: bool = True) -> sqlite3.Row:
        encoded = content.encode("utf-8")
        if len(encoded) > 5 * 1024 * 1024:
            raise ValueError("note exceeds 5 MB sync limit")
        existing = self.get_note(paper_id)
        current = int(existing["version"] or 1) if existing else 0
        if base_version is not None and int(base_version) != current:
            raise RuntimeError("version conflict")
        version = current + 1
        now = time.time()
        digest = hashlib.sha256(encoded).hexdigest()
        device = os.environ.get("PAPERLAB_DEVICE_ID", "local")
        self.conn.execute(
            """INSERT INTO paper_notes
               (paper_id,note_path,content_hash,updated_at,version,updated_by_device,sync_error)
               VALUES (?,?,?,?,?,?,NULL) ON CONFLICT(paper_id) DO UPDATE SET
               note_path=excluded.note_path,content_hash=excluded.content_hash,
               updated_at=excluded.updated_at,version=excluded.version,
               updated_by_device=excluded.updated_by_device,sync_error=NULL,deleted_at=NULL""",
            (paper_id, note_path, digest, now, version, device),
        )
        self.conn.execute(
            """INSERT OR REPLACE INTO content_versions
               (entity_type,entity_id,version,content,content_hash,created_at,device_id)
               VALUES ('note',?,?,?,?,?,?)""",
            (paper_id, version, content, digest, now, device),
        )
        if track_change:
            self.record_change("note", paper_id, "update", {
                "content": content, "content_hash": digest, "updated_at": now,
            }, base_version=current, version=version)
        self.conn.commit()
        return self.get_note(paper_id)

    def set_report(self, paper_id: str, *, report_path: str, status: str = "ok",
                   commit: bool = True) -> None:
        now = time.time()
        existing = self.get_report(paper_id)
        current = int(existing["version"] or 1) if existing else 0
        version = current + 1
        device = os.environ.get("PAPERLAB_DEVICE_ID", "local")
        self.conn.execute(
            """INSERT INTO paper_reports (paper_id,report_path,status,generated_at,updated_at,version,updated_by_device)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(paper_id) DO UPDATE SET
                 report_path=excluded.report_path,status=excluded.status,
                 updated_at=excluded.updated_at,version=excluded.version,
                 updated_by_device=excluded.updated_by_device""",
            (paper_id, str(report_path), status, now, now, version, device),
        )
        path = Path(report_path)
        if status == "ok" and path.is_file():
            content = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(content.encode()).hexdigest()
            objects = self.markdown_objects(content, path)
            self.conn.execute(
                """INSERT OR IGNORE INTO content_versions
                   (entity_type,entity_id,version,content,content_hash,created_at,device_id)
                   VALUES ('report',?,?,?,?,?,?)""",
                (paper_id, version, content, digest, now, device),
            )
            self.record_change("report", paper_id, "update",
                               {"content": content, "content_hash": digest, "objects": objects},
                               base_version=current, version=version)
        if commit:
            self.conn.commit()

    def list_collection_topics(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT t.*,count(tp.paper_id) AS paper_count,
                      COALESCE(r.status,'none') AS review_status
               FROM collection_topics t
               LEFT JOIN collection_topic_papers tp ON tp.topic_id=t.id
               LEFT JOIN topic_reviews r ON r.topic_id=t.id
               WHERE t.deleted_at IS NULL GROUP BY t.id ORDER BY lower(t.name),t.id"""
        ).fetchall()

    def get_collection_topic(self, topic_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT t.*,count(tp.paper_id) AS paper_count,
                      COALESCE(r.status,'none') AS review_status
               FROM collection_topics t
               LEFT JOIN collection_topic_papers tp ON tp.topic_id=t.id
               LEFT JOIN topic_reviews r ON r.topic_id=t.id
               WHERE t.id=? AND t.deleted_at IS NULL GROUP BY t.id""", (topic_id,)
        ).fetchone()

    def create_collection_topic(self, name: str) -> sqlite3.Row:
        clean = str(name or "").strip()
        if not clean:
            raise ValueError("topic name cannot be empty")
        now = time.time()
        import uuid
        sync_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO collection_topics(name,created_at,updated_at,sync_id,updated_by_device) VALUES (?,?,?,?,?)",
            (clean, now, now, sync_id, os.environ.get("PAPERLAB_DEVICE_ID", "local")),
        )
        topic_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.record_change("topic", sync_id, "create", {"name": clean}, version=1)
        self.conn.commit()
        return self.get_collection_topic(topic_id)

    def delete_collection_topic(self, topic_id: int) -> dict | None:
        topic = self.get_collection_topic(topic_id)
        if topic is None:
            return None
        review = self.get_topic_review(topic_id)
        now = time.time()
        current = int(topic["version"] or 1)
        self.conn.execute("UPDATE collection_topics SET deleted_at=?,version=? WHERE id=?",
                          (now, current + 1, topic_id))
        self.conn.execute("DELETE FROM collection_topic_papers WHERE topic_id=?", (topic_id,))
        self.conn.execute("DELETE FROM topic_reviews WHERE topic_id=?", (topic_id,))
        self.record_change("topic", topic["sync_id"], "delete", {},
                           base_version=current, version=current + 1)
        self.conn.commit()
        return {"topic": topic, "review": review}

    def add_paper_to_collection(self, topic_id: int, paper_id: str) -> bool:
        if self.get_collection_topic(topic_id) is None:
            raise KeyError("topic not found")
        if self.get(paper_id) is None:
            raise LookupError("paper not found")
        before = self.conn.total_changes
        now = time.time()
        self.conn.execute(
            "INSERT OR IGNORE INTO collection_topic_papers(topic_id,paper_id,added_at) VALUES (?,?,?)",
            (topic_id, paper_id, now),
        )
        changed = self.conn.total_changes > before
        if changed:
            self.conn.execute("UPDATE collection_topics SET updated_at=? WHERE id=?", (now, topic_id))
            self._mark_topic_review_stale(topic_id, commit=False)
            topic = self.get_collection_topic(topic_id)
            self.record_change("topic_membership", f"{topic['sync_id']}:{paper_id}", "create",
                               {"topic_sync_id": topic["sync_id"], "paper_id": paper_id})
        self.conn.commit()
        return changed

    def remove_paper_from_collection(self, topic_id: int, paper_id: str) -> bool:
        if self.get_collection_topic(topic_id) is None:
            raise KeyError("topic not found")
        before = self.conn.total_changes
        self.conn.execute(
            "DELETE FROM collection_topic_papers WHERE topic_id=? AND paper_id=?",
            (topic_id, paper_id),
        )
        changed = self.conn.total_changes > before
        if changed:
            now = time.time()
            self.conn.execute("UPDATE collection_topics SET updated_at=? WHERE id=?", (now, topic_id))
            self._mark_topic_review_stale(topic_id, commit=False)
            topic = self.get_collection_topic(topic_id)
            self.record_change("topic_membership", f"{topic['sync_id']}:{paper_id}", "delete",
                               {"topic_sync_id": topic["sync_id"], "paper_id": paper_id})
        self.conn.commit()
        return changed

    def collection_papers(self, topic_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT p.*,tp.added_at
               FROM collection_topic_papers tp JOIN papers p ON p.id=tp.paper_id
               WHERE tp.topic_id=? ORDER BY COALESCE(p.year,0),lower(p.title),p.id""",
            (topic_id,),
        ).fetchall()

    def paper_collection_ids(self, paper_id: str) -> list[int]:
        return [r[0] for r in self.conn.execute(
            "SELECT topic_id FROM collection_topic_papers WHERE paper_id=? ORDER BY topic_id",
            (paper_id,),
        )]

    def get_topic_review(self, topic_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM topic_reviews WHERE topic_id=?", (topic_id,)
        ).fetchone()

    def set_topic_review(self, topic_id: int, *, status: str, stage: str = "",
                         completed_papers: int = 0, total_papers: int = 0,
                         error: str | None = None, review_path: str | None = None,
                         snapshot_hash: str | None = None, generated: bool = False,
                         commit: bool = True) -> None:
        now = time.time()
        existing = self.get_topic_review(topic_id)
        current = int(existing["version"] or 1) if existing else 0
        version = current + 1 if generated else max(current, 1)
        device = os.environ.get("PAPERLAB_DEVICE_ID", "local")
        self.conn.execute(
            """INSERT INTO topic_reviews
               (topic_id,review_path,status,stage,completed_papers,total_papers,error,
                snapshot_hash,generated_at,updated_at,version,updated_by_device)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(topic_id) DO UPDATE SET
                 review_path=COALESCE(excluded.review_path,topic_reviews.review_path),
                 status=excluded.status,stage=excluded.stage,
                 completed_papers=excluded.completed_papers,total_papers=excluded.total_papers,
                 error=excluded.error,snapshot_hash=COALESCE(excluded.snapshot_hash,topic_reviews.snapshot_hash),
                 generated_at=COALESCE(excluded.generated_at,topic_reviews.generated_at),
                 updated_at=excluded.updated_at,version=excluded.version,
                 updated_by_device=excluded.updated_by_device""",
            (topic_id, review_path, status, stage, completed_papers, total_papers,
             error, snapshot_hash, now if generated else None, now, version, device),
        )
        if generated and review_path and Path(review_path).is_file():
            content = Path(review_path).read_text(encoding="utf-8")
            digest = hashlib.sha256(content.encode()).hexdigest()
            objects = self.markdown_objects(content, review_path)
            topic = self.get_collection_topic(topic_id)
            entity_id = topic["sync_id"] if topic else str(topic_id)
            self.conn.execute(
                """INSERT OR IGNORE INTO content_versions
                   (entity_type,entity_id,version,content,content_hash,created_at,device_id)
                   VALUES ('review',?,?,?,?,?,?)""",
                (entity_id, version, content, digest, now, device),
            )
            self.record_change("review", entity_id, "update",
                               {"content": content, "content_hash": digest, "objects": objects},
                               base_version=current, version=version)
        if commit:
            self.conn.commit()

    def _mark_topic_review_stale(self, topic_id: int, *, commit: bool = True) -> None:
        now = time.time()
        self.conn.execute(
            """UPDATE topic_reviews SET status=CASE WHEN status='running' THEN status ELSE 'stale' END,
                      updated_at=? WHERE topic_id=?""", (now, topic_id)
        )
        if commit:
            self.conn.commit()
