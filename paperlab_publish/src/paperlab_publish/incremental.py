"""HTTP change-journal synchronization for offline PaperLab replicas."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from urllib.parse import quote

import requests

from .auth import AuthManager
from .store import Store


def change_dict(row) -> dict:
    value = dict(row)
    value["changed_fields"] = json.loads(value["changed_fields"] or "{}")
    return value


def authenticate_device(store: Store, authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return store.device_for_token(AuthManager.token_hash(authorization[7:].strip()))


def _changed_since(store: Store, entity_type: str, entity_id: str, base_version: int) -> set[str]:
    fields: set[str] = set()
    rows = store.conn.execute(
        """SELECT changed_fields FROM sync_changes WHERE entity_type=? AND entity_id=?
           AND version>? ORDER BY version""", (entity_type, entity_id, base_version)
    )
    for row in rows:
        fields.update(json.loads(row[0] or "{}").keys())
    return fields


def _topic(store: Store, sync_id: str):
    return store.conn.execute("SELECT * FROM collection_topics WHERE sync_id=?", (sync_id,)).fetchone()


def apply_change(store: Store, change: dict, cfg, *, remote=True) -> list[str]:
    """Apply non-conflicting fields and retain both values for conflicts."""
    entity_type = change["entity_type"]
    entity_id = str(change["entity_id"])
    operation = change["operation"]
    fields = change.get("changed_fields") or {}
    base = int(change.get("base_version") or 0)
    incoming_version = int(change.get("version") or 1)
    device = str(change.get("device_id") or "remote")
    now = time.time()
    conflicts: list[str] = []

    if entity_type == "report":
        return ["legacy_report_rejected"]

    if entity_type == "paper":
        row = store.get(entity_id)
        if row is None:
            if operation == "create" and fields.get("title"):
                store.upsert_candidate({**fields, "title": fields["title"]}, commit=False, track_change=False)
            return conflicts
        current = int(row["version"] or 1)
        overlap = _changed_since(store, entity_type, entity_id, base) & set(fields) if current > base else set()
        allowed = {"title", "authors", "organizations", "abstract", "keywords", "doi", "arxiv_id",
                   "source_url", "pdf_url", "awards", "venue", "year", "track", "citation_count",
                   "published_date", "metadata_status", "github_url", "target_problem", "core_idea",
                   "figure_kind", "figure_mermaid", "extraction_version", "relevance_score",
                   "relevance_reason"}
        updates = {k: v for k, v in fields.items() if k in allowed and k not in overlap}
        for field in overlap:
            store.conn.execute(
                """INSERT INTO sync_conflicts(entity_type,entity_id,field,local_value,remote_value,
                   local_version,remote_version,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (entity_type, entity_id, field, json.dumps(row[field], ensure_ascii=False),
                 json.dumps(fields[field], ensure_ascii=False), current, incoming_version, now),
            )
            conflicts.append(field)
        if operation == "delete":
            store.conn.execute("UPDATE papers SET deleted_at=?,version=?,updated_by_device=? WHERE id=?",
                               (now, max(current, incoming_version), device, entity_id))
        elif updates:
            store.conn.execute(
                "UPDATE papers SET " + ",".join(f"{k}=?" for k in updates) +
                ",version=?,updated_by_device=?,last_updated=? WHERE id=?",
                (*updates.values(), max(current + 1, incoming_version), device, now, entity_id),
            )
        digest = fields.get("object_sha256")
        if digest:
            obj = store.conn.execute("SELECT storage_path FROM stored_objects WHERE sha256=?", (digest,)).fetchone()
            if obj:
                store.conn.execute(
                    "UPDATE papers SET pdf_path=?,pdf_status='ok',version=?,updated_by_device=? WHERE id=?",
                    (obj[0], max(current + 1, incoming_version), device, entity_id),
                )

    elif entity_type == "topic":
        row = _topic(store, entity_id)
        if row is None and operation == "create":
            store.conn.execute(
                """INSERT INTO collection_topics(name,created_at,updated_at,sync_id,version,updated_by_device)
                   VALUES (?,?,?,?,?,?)""",
                (str(fields.get("name") or "Untitled"), now, now, entity_id, incoming_version, device),
            )
        elif row is not None:
            current = int(row["version"] or 1)
            if operation == "delete":
                store.conn.execute("UPDATE collection_topics SET deleted_at=?,version=? WHERE sync_id=?",
                                   (now, max(current, incoming_version), entity_id))
            elif "name" in fields:
                overlap = "name" in _changed_since(store, entity_type, entity_id, base) and current > base
                if overlap:
                    store.conn.execute(
                        """INSERT INTO sync_conflicts(entity_type,entity_id,field,local_value,remote_value,
                           local_version,remote_version,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                        (entity_type, entity_id, "name", json.dumps(row["name"]), json.dumps(fields["name"]),
                         current, incoming_version, now),
                    )
                    conflicts.append("name")
                else:
                    store.conn.execute(
                        "UPDATE collection_topics SET name=?,updated_at=?,version=?,updated_by_device=? WHERE sync_id=?",
                        (fields["name"], now, max(current + 1, incoming_version), device, entity_id),
                    )

    elif entity_type == "topic_membership":
        topic = _topic(store, fields.get("topic_sync_id", ""))
        paper_id = fields.get("paper_id")
        if topic and store.get(paper_id):
            if operation == "delete":
                store.conn.execute("DELETE FROM collection_topic_papers WHERE topic_id=? AND paper_id=?",
                                   (topic["id"], paper_id))
            else:
                store.conn.execute(
                    "INSERT OR IGNORE INTO collection_topic_papers(topic_id,paper_id,added_at) VALUES (?,?,?)",
                    (topic["id"], paper_id, now),
                )

    elif entity_type == "paper_topic_score":
        paper_id, topic_id = fields.get("paper_id"), fields.get("topic_id")
        if paper_id and topic_id and store.get(paper_id):
            store.conn.execute(
                """INSERT INTO paper_topics
                   (paper_id,topic_id,score,relevant,reason,model,definition_hash,prompt_version,scored_at)
                   VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(paper_id,topic_id) DO UPDATE SET
                   score=excluded.score,relevant=excluded.relevant,reason=excluded.reason,model=excluded.model,
                   definition_hash=excluded.definition_hash,prompt_version=excluded.prompt_version,
                   scored_at=excluded.scored_at""",
                (paper_id, topic_id, fields.get("score", 0), fields.get("relevant", 0),
                 fields.get("reason"), fields.get("model"), fields.get("definition_hash", ""),
                 fields.get("prompt_version", 0), now),
            )

    elif entity_type == "paper_venue":
        if fields.get("paper_id") and fields.get("venue") and fields.get("year") and store.get(fields["paper_id"]):
            store.upsert_venue(fields["paper_id"], venue=fields["venue"], year=int(fields["year"]),
                               track=fields.get("track"), source=fields.get("source") or "sync")

    elif entity_type == "paper_summary":
        if not store.get(entity_id): return conflicts
        row = store.conn.execute("SELECT * FROM paper_summaries WHERE paper_id=?", (entity_id,)).fetchone()
        current = int(row["version"] or 1) if row else 0
        if incoming_version >= current:
            store.conn.execute(
                """INSERT INTO paper_summaries
                   (paper_id,content,abstract_hash,status,provider,model,generated_at,updated_at,version,updated_by_device)
                   VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET
                   content=excluded.content,abstract_hash=excluded.abstract_hash,status=excluded.status,
                   provider=excluded.provider,model=excluded.model,generated_at=excluded.generated_at,
                   updated_at=excluded.updated_at,version=excluded.version,updated_by_device=excluded.updated_by_device,deleted_at=NULL""",
                (entity_id,fields.get("content",""),fields.get("abstract_hash",""),fields.get("status","ok"),
                 fields.get("provider"),fields.get("model"),fields.get("generated_at"),now,incoming_version,device))

    elif entity_type == "note" and "content" in fields:
        if not store.get(entity_id): return conflicts
        import hashlib, re as _re
        content = str(fields.get("content") or "")
        if len(content.encode()) > 5 * 1024 * 1024: return ["note_too_large"]
        digest_id = hashlib.sha256(entity_id.encode()).hexdigest()[:24]
        path = Path(cfg.pdf_root) / "reading-notes" / f"{digest_id}.md"
        row = store.conn.execute("SELECT * FROM paper_notes WHERE paper_id=?", (entity_id,)).fetchone()
        current = int(row["version"] or 1) if row else 0
        local = path.read_text(encoding="utf-8") if path.is_file() else ""
        if row and current != base and local != content:
            safe = _re.sub(r"[^A-Za-z0-9_.-]","_",device)[:48] or "remote"
            conflict = content if current > incoming_version else local
            conflict_path = path.with_name(f"{path.stem}.conflict-{safe}-{time.strftime('%Y%m%d-%H%M%S')}.md")
            conflict_path.parent.mkdir(parents=True,exist_ok=True); conflict_path.write_text(conflict,encoding="utf-8")
            conflicts.append("content")
        if incoming_version >= current:
            path.parent.mkdir(parents=True,exist_ok=True); temp=path.with_suffix(".md.sync-tmp")
            temp.write_text(content,encoding="utf-8"); os.replace(temp,path)
            digest=fields.get("content_hash") or hashlib.sha256(content.encode()).hexdigest()
            store.conn.execute("""INSERT INTO paper_notes(paper_id,note_path,content_hash,updated_at,version,updated_by_device,sync_error)
              VALUES (?,?,?,?,?,?,NULL) ON CONFLICT(paper_id) DO UPDATE SET note_path=excluded.note_path,
              content_hash=excluded.content_hash,updated_at=excluded.updated_at,version=excluded.version,
              updated_by_device=excluded.updated_by_device,sync_error=NULL,deleted_at=NULL""",
              (entity_id,str(path),digest,fields.get("updated_at") or now,incoming_version,device))

    elif entity_type == "review" and "content" in fields:
        if entity_type == "review":
            from .report import topic_review_path_for
            topic = _topic(store, entity_id)
            if not topic:
                return conflicts
            path = topic_review_path_for(cfg, topic["id"])
            row = store.get_topic_review(topic["id"])
            current = int(row["version"] or 1) if row else 0
        if current > base:
            local = path.read_text(encoding="utf-8") if path.is_file() else ""
            store.conn.execute(
                """INSERT INTO sync_conflicts(entity_type,entity_id,field,local_value,remote_value,
                   local_version,remote_version,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (entity_type, entity_id, "content", local, fields["content"], current, incoming_version, now),
            )
            conflicts.append("content")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            report_root = (cfg.pdf_root / "reports").resolve()
            for spec in fields.get("objects") or []:
                obj = store.conn.execute("SELECT storage_path FROM stored_objects WHERE sha256=?",
                                         (spec.get("sha256"),)).fetchone()
                relative = str(spec.get("path") or "")
                destination = (path.parent / relative).resolve()
                if obj and relative and ".." not in Path(relative).parts and destination.is_relative_to(report_root):
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(obj[0], destination)
            temp = path.with_suffix(path.suffix + ".sync-tmp")
            temp.write_text(fields["content"], encoding="utf-8")
            os.replace(temp, path)
            store.conn.execute(
                """INSERT INTO topic_reviews(topic_id,review_path,status,stage,updated_at,version,updated_by_device)
                   VALUES (?,?,'ok','synced',?,?,?) ON CONFLICT(topic_id) DO UPDATE SET review_path=excluded.review_path,
                   status='ok',stage='synced',updated_at=excluded.updated_at,version=excluded.version,
                   updated_by_device=excluded.updated_by_device""",
                (topic["id"], str(path), now, incoming_version, device),
            )
    return conflicts


def push_to_server(store: Store, changes: list[dict], cfg) -> dict:
    accepted = []
    rejected = []
    for incoming in changes:
        if incoming.get("entity_type") == "report":
            rejected.append({"change_id": incoming.get("change_id"), "error": "legacy_report_rejected"})
            continue
        existing = store.conn.execute(
            "SELECT server_sequence FROM sync_changes WHERE change_id=?", (incoming["change_id"],)
        ).fetchone()
        if existing:
            accepted.append({"change_id": incoming["change_id"], "server_sequence": existing[0]})
            continue
        sequence = store.next_server_sequence()
        conflicts = apply_change(store, incoming, cfg)
        store.record_change(
            incoming["entity_type"], incoming["entity_id"], incoming["operation"],
            incoming.get("changed_fields") or {}, base_version=incoming.get("base_version", 0),
            version=incoming.get("version", 1), device_id=incoming.get("device_id", "remote"),
            server_sequence=sequence, change_id=incoming["change_id"], uploaded=True,
        )
        accepted.append({"change_id": incoming["change_id"], "server_sequence": sequence,
                         "conflicts": conflicts})
    store.conn.commit()
    return {"accepted": accepted, "rejected": rejected}


def sync_now(cfg, *, log=print) -> dict:
    base_url = os.environ.get("PAPERLAB_SYNC_URL", "").rstrip("/")
    token = os.environ.get("PAPERLAB_DEVICE_TOKEN", "")
    device_id = os.environ.get("PAPERLAB_DEVICE_ID", "local")
    if not base_url or not token:
        raise RuntimeError("PAPERLAB_SYNC_URL and PAPERLAB_DEVICE_TOKEN are required")
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    with Store(cfg.db_path, backup_on_migrate=False) as store:
        cursor_row = store.conn.execute("SELECT last_server_sequence FROM sync_cursors WHERE device_id=?",
                                        (device_id,)).fetchone()
        cursor = int(cursor_row[0]) if cursor_row else 0

        def pull(after):
            total_pulled = 0
            while True:
                response = requests.get(f"{base_url}/api/sync/pull?after={after}", headers=headers, timeout=60)
                response.raise_for_status()
                payload = response.json()
                for change in payload.get("changes", []):
                    if not store.conn.execute("SELECT 1 FROM sync_changes WHERE change_id=?", (change["change_id"],)).fetchone():
                        change_fields = change.get("changed_fields") or {}
                        digests = [change_fields.get("object_sha256")]
                        digests.extend(x.get("sha256") for x in change_fields.get("objects") or [])
                        for digest in [x for x in digests if x]:
                            if not store.conn.execute("SELECT 1 FROM stored_objects WHERE sha256=?", (digest,)).fetchone():
                                obj_response = requests.get(f"{base_url}/api/sync/objects/{digest}", headers=headers, timeout=300)
                                obj_response.raise_for_status()
                                object_dir = cfg.root / "data" / "objects"
                                object_dir.mkdir(parents=True, exist_ok=True)
                                target = object_dir / digest
                                temp = target.with_suffix(".sync-tmp")
                                temp.write_bytes(obj_response.content)
                                import hashlib
                                if hashlib.sha256(obj_response.content).hexdigest() != digest:
                                    temp.unlink(missing_ok=True)
                                    raise RuntimeError("downloaded object hash mismatch")
                                os.replace(temp, target)
                                store.conn.execute(
                                    """INSERT INTO stored_objects(sha256,size,content_type,storage_path,created_at)
                                       VALUES (?,?,?,?,?)""",
                                    (digest, target.stat().st_size, obj_response.headers.get("Content-Type", "application/octet-stream"),
                                     str(target), time.time()),
                                )
                        apply_change(store, change, cfg)
                        store.record_change(change["entity_type"], change["entity_id"], change["operation"],
                                            change.get("changed_fields") or {}, base_version=change.get("base_version", 0),
                                            version=change.get("version", 1), device_id=change.get("device_id", "server"),
                                            server_sequence=change["server_sequence"], change_id=change["change_id"], uploaded=True)
                total_pulled += len(payload.get("changes", []))
                after = int(payload.get("latest_sequence", after))
                store.conn.execute(
                    """INSERT INTO sync_cursors(device_id,last_server_sequence,updated_at) VALUES (?,?,?)
                       ON CONFLICT(device_id) DO UPDATE SET last_server_sequence=excluded.last_server_sequence,
                       updated_at=excluded.updated_at""", (device_id, after, time.time())
                )
                store.conn.commit()
                if not payload.get("has_more"):
                    return after, total_pulled

        cursor, pulled1 = pull(cursor)
        # Upload content-addressed objects before changes that reference them.
        pending = [change_dict(row) for row in store.pending_changes()]
        required_objects = set()
        for change in pending:
            fields = change.get("changed_fields") or {}
            if fields.get("object_sha256"):
                required_objects.add(fields["object_sha256"])
            required_objects.update(x.get("sha256") for x in fields.get("objects") or [] if x.get("sha256"))
        for obj in store.conn.execute("SELECT * FROM stored_objects WHERE deleted_at IS NULL"):
            digest = obj["sha256"]
            if digest not in required_objects:
                continue
            head = requests.head(f"{base_url}/api/sync/objects/{digest}", headers=headers, timeout=30)
            if head.status_code == 404:
                path = Path(obj["storage_path"])
                offset = int(head.headers.get("X-Upload-Offset", "0"))
                total = path.stat().st_size
                with path.open("rb") as handle:
                    handle.seek(offset)
                    while True:
                        chunk = handle.read(4 * 1024 * 1024)
                        if not chunk:
                            break
                        response = requests.put(
                            f"{base_url}/api/sync/objects/{digest}", data=chunk,
                            headers={**headers, "Content-Type": obj["content_type"],
                                     "X-Upload-Offset": str(offset), "X-Upload-Total": str(total)}, timeout=120)
                        response.raise_for_status()
                        offset += len(chunk)
            elif head.status_code != 200:
                head.raise_for_status()
        pushed = 0
        batches = []
        batch = []
        batch_bytes = 0
        for change in pending:
            change_bytes = len(json.dumps(change, ensure_ascii=False).encode())
            if batch and (len(batch) >= 500 or batch_bytes + change_bytes > 4 * 1024 * 1024):
                batches.append(batch); batch = []; batch_bytes = 0
            batch.append(change); batch_bytes += change_bytes
        if batch:
            batches.append(batch)
        for batch in batches:
            response = requests.post(base_url + "/api/sync/push", headers=headers,
                                     json={"changes": batch}, timeout=120)
            response.raise_for_status()
            for item in response.json().get("accepted", []):
                store.conn.execute("UPDATE sync_changes SET uploaded_at=?,server_sequence=? WHERE change_id=?",
                                   (time.time(), item["server_sequence"], item["change_id"]))
                pushed += 1
            store.conn.commit()
        cursor, pulled2 = pull(cursor)
    result = {"pulled": pulled1 + pulled2, "pushed": pushed, "cursor": cursor}
    with Store(cfg.db_path, backup_on_migrate=False) as runtime_store:
        runtime_store.conn.execute(
            """INSERT INTO sync_runtime(id,last_success,last_error,last_attempt) VALUES (1,?,NULL,?)
               ON CONFLICT(id) DO UPDATE SET last_success=excluded.last_success,last_error=NULL,
               last_attempt=excluded.last_attempt""", (time.time(), time.time())
        )
        runtime_store.conn.commit()
    log(json.dumps(result, ensure_ascii=False))
    return result
