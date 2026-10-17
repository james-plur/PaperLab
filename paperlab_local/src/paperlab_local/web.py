"""Fast local Web UI backed directly by paginated SQLite queries."""
from __future__ import annotations
import json
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
def _connect(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def reconcile_pdf_paths(db_path, pdf_root):
    """Repair portable PDF links after a database or data-directory migration."""
    pdf_dir = Path(pdf_root).resolve() / "pdfs"
    if not pdf_dir.is_dir():
        return 0
    available = {path.stem: path.resolve() for path in pdf_dir.rglob("*.pdf")
                 if path.is_file() and path.stat().st_size >= 1000}
    conn = _connect(db_path)
    repaired = 0
    try:
        for row in conn.execute("SELECT id,pdf_path FROM papers WHERE deleted_at IS NULL"):
            current = Path(row["pdf_path"]) if row["pdf_path"] else None
            if current and current.is_file():
                continue
            key = row["id"].replace(":", "-").replace("/", "_")
            candidate = available.get(key)
            if candidate:
                conn.execute("UPDATE papers SET pdf_path=?,pdf_status='ok' WHERE id=?",
                             (str(candidate), row["id"]))
                repaired += 1
        conn.commit()
    finally:
        conn.close()
    return repaired

_TOPIC_TASKS_LOCK = threading.Lock()
_TOPIC_TASKS: set[tuple[str, int]] = set()

def _paper_select(alias="p"):
    return (
        f"{alias}.id,{alias}.title,{alias}.authors,{alias}.organizations,{alias}.awards,"
        f"{alias}.venue,{alias}.year,{alias}.abstract,{alias}.doi,{alias}.arxiv_id,"
        f"{alias}.source_url,{alias}.pdf_url,{alias}.pdf_path,{alias}.metadata_status,{alias}.version,"
        f"{alias}.citation_count,{alias}.keywords,"
        f"(SELECT group_concat(pv.venue || ' ' || pv.year,'|') FROM paper_venues pv WHERE pv.paper_id={alias}.id) venue_memberships,"
        f"(SELECT group_concat(ctp.topic_id,',') FROM collection_topic_papers ctp WHERE ctp.paper_id={alias}.id) collection_topic_ids,"
        f"(SELECT pr.report_path FROM paper_reports pr WHERE pr.paper_id={alias}.id AND pr.status='ok') report_path,"
        f"(SELECT pr.status FROM paper_reports pr WHERE pr.paper_id={alias}.id) report_status"
    )

def _paper_dict(row):
    value = dict(row)
    pdf_path = value.pop("pdf_path", None)
    value["has_pdf"] = bool(pdf_path and os.path.isfile(pdf_path))
    value["pdf_path"] = None  # compatibility without exposing host paths
    report_path = value.pop("report_path", None)
    value["has_report"] = bool(
        value.pop("report_status", None) == "ok" and report_path and os.path.isfile(report_path))
    return value
def query_papers(db_path, params):
    def one(name, default=""):
        return str((params.get(name) or [default])[0]).strip()
    page = max(1, int(one("page", "1") or 1))
    per_page = min(100, max(10, int(one("per_page", "50") or 50)))
    clauses = ["p.deleted_at IS NULL"]
    values = []
    q = one("q")
    if q:
        clauses.append("(p.title LIKE ? OR p.authors LIKE ? OR p.organizations LIKE ? "
                       "OR p.abstract LIKE ? OR p.keywords LIKE ?)")
        token = "%" + q + "%"
        values.extend([token] * 5)
    venue = one("venue").upper()
    year = one("year")
    if venue and year.isdigit():
        clauses.append("EXISTS (SELECT 1 FROM paper_venues pv WHERE pv.paper_id=p.id AND pv.venue=? AND pv.year=?)")
        values.extend([venue, int(year)])
    elif venue:
        clauses.append("EXISTS (SELECT 1 FROM paper_venues pv WHERE pv.paper_id=p.id AND pv.venue=?)")
        values.append(venue)
    elif year.isdigit():
        clauses.append("EXISTS (SELECT 1 FROM paper_venues pv WHERE pv.paper_id=p.id AND pv.year=?)")
        values.append(int(year))
    org = one("org")
    if org:
        clauses.append("p.normalized_orgs LIKE ?")
        values.append('%"' + org + '"%')
    if one("award") in {"1", "true", "yes"}:
        clauses.append("p.awards IS NOT NULL AND trim(p.awards) != ''")
    sort = one("sort", "year_desc")
    order_by = {
        "year_desc": "COALESCE(p.year,0) DESC,p.title",
        "year_asc": "COALESCE(p.year,0),p.title",
        "citations_desc": "COALESCE(p.citation_count,0) DESC,p.title",
        "title_asc": "lower(p.title),p.id",
        "updated_desc": "p.last_updated DESC,p.title",
    }.get(sort, "COALESCE(p.year,0) DESC,p.title")
    where = " AND ".join(clauses)
    conn = _connect(db_path)
    try:
        total = conn.execute("SELECT count(*) FROM papers p WHERE " + where, values).fetchone()[0]
        rows = conn.execute(
            "SELECT " + _paper_select("p") + ","
            "(SELECT group_concat(pt.topic_id || ':' || printf('%.2f',pt.score),'|') FROM paper_topics pt WHERE pt.paper_id=p.id AND pt.relevant=1) topics "
            "FROM papers p WHERE " + where + " ORDER BY " + order_by + " LIMIT ? OFFSET ?",
            [*values, per_page, (page - 1) * per_page]).fetchall()
    finally:
        conn.close()
    return {"page": page, "per_page": per_page, "total": total,
            "pages": (total + per_page - 1) // per_page,
            "papers": [_paper_dict(r) for r in rows]}
def query_facets(db_path):
    conn = _connect(db_path)
    try:
        total = conn.execute("SELECT count(*) FROM papers WHERE deleted_at IS NULL").fetchone()[0]
        award_count = conn.execute(
            "SELECT count(*) FROM papers WHERE deleted_at IS NULL AND awards IS NOT NULL AND trim(awards) != ''"
        ).fetchone()[0]
        venues = [r[0] for r in conn.execute("SELECT DISTINCT venue FROM paper_venues WHERE venue!='' ORDER BY venue")]
        years = [r[0] for r in conn.execute("SELECT DISTINCT year FROM paper_venues ORDER BY year DESC")]
        import json
        from collections import Counter
        org_counts = Counter()
        rows = conn.execute("SELECT normalized_orgs FROM papers WHERE normalized_orgs IS NOT NULL")
        for r in rows:
            try:
                for o in json.loads(r[0]):
                    org_counts[o] += 1
            except (ValueError, TypeError):
                pass
        orgs = [o for o, _ in org_counts.most_common(200)]
    finally:
        conn.close()
    return {"total": total, "award_count": award_count,
            "venues": venues, "years": years, "orgs": orgs}

def query_collection_papers(db_path, topic_id):
    conn = _connect(db_path)
    try:
        topic = conn.execute("SELECT * FROM collection_topics WHERE id=?", (topic_id,)).fetchone()
        if topic is None:
            return None
        rows = conn.execute(
            "SELECT " + _paper_select("p") +
            " FROM collection_topic_papers ctp JOIN papers p ON p.id=ctp.paper_id "
            "WHERE ctp.topic_id=? AND p.deleted_at IS NULL ORDER BY COALESCE(p.year,0),lower(p.title),p.id",
            (topic_id,),
        ).fetchall()
        return {"topic": dict(topic), "papers": [_paper_dict(r) for r in rows], "total": len(rows)}
    finally:
        conn.close()

def search_papers(db_path, params):
    """Semantic search that respects venue and year filters."""
    import re as _re
    def one(name, default=""):
        return str((params.get(name) or [default])[0]).strip()
    description = one("q")
    if not description:
        return []
    desc_lower = description.lower().strip()
    search_kws = set()
    tokens = _re.split(r"[\s,;:!?\]\[()]+", desc_lower)
    en_words = []
    for token in tokens:
        if not token:
            continue
        en_part = _re.sub(r"[^\x00-\x7f]", "", token)
        if en_part and len(en_part) > 1:
            en_words.append(en_part)
            search_kws.add(en_part)
        cn_segs = _re.findall(r"[\u4e00-\u9fff]+", token)
        for seg in cn_segs:
            if len(seg) >= 2:
                for i in range(len(seg) - 1):
                    search_kws.add(seg[i:i+2])
            elif len(seg) == 1:
                search_kws.add(seg)
    for i in range(len(en_words) - 1):
        search_kws.add(en_words[i] + " " + en_words[i + 1])
    for i in range(len(en_words) - 2):
        search_kws.add(en_words[i] + " " + en_words[i + 1] + " " + en_words[i + 2])
    variants = set()
    for kw in search_kws:
        if " " in kw:
            no_space = kw.replace(" ", "")
            if len(no_space) > 3:
                variants.add(no_space)
        if "kv" in kw and "cache" in kw and "kvcache" not in kw:
            variants.add("kvcache")
    search_kws.update(variants)
    if not search_kws:
        return []
    kw_list = list(search_kws)[:20]
    venue = one("venue").upper()
    year = one("year")
    venue_filter = " AND papers.deleted_at IS NULL"
    venue_params = []
    if venue and year.isdigit():
        venue_filter = "AND EXISTS (SELECT 1 FROM paper_venues pv WHERE pv.paper_id=papers.id AND pv.venue=? AND pv.year=?)"
        venue_params = [venue, int(year)]
    elif venue:
        venue_filter = "AND EXISTS (SELECT 1 FROM paper_venues pv WHERE pv.paper_id=papers.id AND pv.venue=?)"
        venue_params = [venue]
    elif year.isdigit():
        venue_filter = "AND EXISTS (SELECT 1 FROM paper_venues pv WHERE pv.paper_id=papers.id AND pv.year=?)"
        venue_params = [int(year)]
    org = one("org")
    if org:
        venue_filter += ' AND papers.normalized_orgs LIKE ?'
        venue_params.append('%"' + org + '"%')
    conn = _connect(db_path)
    candidates = []
    seen_ids = set()
    try:
        for kw in kw_list:
            token = "%" + kw + "%"
            rows = conn.execute(
                "SELECT " + _paper_select("papers") +
                " FROM papers WHERE keywords IS NOT NULL AND lower(keywords) LIKE ? "
                + venue_filter + " ORDER BY year DESC,title LIMIT 30", [token] + venue_params).fetchall()
            for r in rows:
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    candidates.append(_paper_dict(r))
        for kw in kw_list:
            token = "%" + kw + "%"
            rows = conn.execute(
                "SELECT " + _paper_select("papers") + " FROM papers WHERE lower(title) LIKE ? "
                + venue_filter + " ORDER BY year DESC,title LIMIT 30", [token] + venue_params).fetchall()
            for r in rows:
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    candidates.append(_paper_dict(r))
        for kw in kw_list[:5]:
            token = "%" + kw + "%"
            rows = conn.execute(
                "SELECT " + _paper_select("papers") + " FROM papers WHERE lower(abstract) LIKE ? "
                + venue_filter + " ORDER BY year DESC,title LIMIT 30", [token] + venue_params).fetchall()
            for r in rows:
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    candidates.append(_paper_dict(r))
    finally:
        conn.close()
    if not candidates:
        return []
    for c in candidates:
        hits = 0
        text = ((c.get("title") or "") + " " + (c.get("keywords") or "") + " " + (c.get("abstract") or "")).lower()
        text_nospace = text.replace(" ", "")
        for kw in kw_list:
            kw_nospace = kw.replace(" ", "")
            if kw in text or (len(kw_nospace) > 3 and kw_nospace in text_nospace):
                hits += 1
        c["match_score"] = round(hits / max(1, len(kw_list)), 2)
        c["match_reason"] = str(hits) + "/" + str(len(kw_list)) + " matched"
        if c.get("abstract"):
            c["abstract"] = c["abstract"][:400]
    candidates.sort(key=lambda x: (-x.get("match_score", 0), -x.get("year", 0)))
    return candidates[:100]

def update_paper(db_path, paper_id, fields, base_version=None):
    allowed = ("title", "authors", "organizations", "abstract", "keywords",
               "doi", "arxiv_id", "venue", "year", "source_url", "pdf_url", "awards")
    from .store import Store
    with Store(str(db_path), backup_on_migrate=False) as store:
        conn = store.conn
        row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        if row is None:
            return {"updated": 0}
        current = int(row["version"] or 1)
        if base_version is not None and int(base_version) != current:
            raise RuntimeError("version conflict")
        candidate_title = str(fields.get("title", row["title"]) or "").strip()
        candidate_doi = str(fields.get("doi", row["doi"]) or "").strip().lower()
        candidate_arxiv = str(fields.get("arxiv_id", row["arxiv_id"]) or "").strip()
        from .store import _clean_arxiv, title_hash
        candidate_arxiv = _clean_arxiv(candidate_arxiv) or ""
        duplicate = conn.execute(
            """SELECT id,title FROM papers WHERE id!=? AND deleted_at IS NULL AND
               ((?!='' AND lower(doi)=?) OR (?!='' AND arxiv_id=?) OR norm_title_hash=?) LIMIT 1""",
            (paper_id, candidate_doi, candidate_doi, candidate_arxiv, candidate_arxiv,
             title_hash(candidate_title)),
        ).fetchone()
        if duplicate:
            raise ValueError(f"duplicate paper: {duplicate['title']} ({duplicate['id']})")
        sets = []
        vals = []
        changed = {}
        for k in allowed:
            if k in fields:
                sets.append(k + "=?")
                value = fields[k] if fields[k] else None
                vals.append(value)
                changed[k] = value
        if "title" in fields:
            sets.append("norm_title_hash=?")
            vals.append(title_hash(candidate_title))
        if "doi" in fields:
            changed["doi"] = candidate_doi or None
            vals[next(i for i, item in enumerate(sets) if item == "doi=?")] = candidate_doi or None
        if "arxiv_id" in fields:
            index = next(i for i, item in enumerate(sets) if item == "arxiv_id=?")
            vals[index] = candidate_arxiv or None
            changed["arxiv_id"] = candidate_arxiv or None
        if not sets:
            return {"updated": 0}
        sets.append("last_updated=?")
        vals.append(time.time())
        sets.extend(["version=?", "updated_by_device=?"])
        vals.extend([current + 1, os.environ.get("PAPERLAB_DEVICE_ID", "local")])
        vals.append(paper_id)
        conn.execute("UPDATE papers SET " + ",".join(sets) + " WHERE id=?", vals)
        store.record_activity(paper_id, "metadata_edited", {"fields": sorted(changed)})
        store.record_change("paper", paper_id, "update", changed,
                            base_version=current, version=current + 1)
        conn.commit()
        row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        return {"updated": 1, "paper": dict(row) if row else None}


class PaperHandler(BaseHTTPRequestHandler):
    db_path = Path(".")
    config_path = Path(__file__).resolve().parent.parent / "topics.yaml"
    app_token = ""
    no_browser_ui = False

    def _send(self, body, content_type, status=200, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if self.app_token:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization,Content-Type,X-PaperLab-Provider,X-PaperLab-Model")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        if self.app_token:
            self._send(b"", "text/plain", 204)
        else:
            self._send(b"", "text/plain", 404)

    def _trusted_local(self):
        enabled = os.environ.get("PAPERLAB_LOCAL_TRUSTED_MODE", "1").lower() in {"1", "true", "yes"}
        return enabled and self.client_address[0] in {"127.0.0.1", "::1"}

    def _app_authenticated(self):
        if not self.app_token:
            return False
        auth = self.headers.get("Authorization", "")
        import hmac
        return auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), self.app_token)

    def _session(self):
        from .auth import AUTH
        return AUTH.from_cookie(self.headers.get("Cookie"))

    def _is_admin(self):
        return self._app_authenticated() or self._trusted_local() or self._session() is not None

    def _require_admin(self, *, csrf=True):
        if self._app_authenticated():
            return True
        if self._trusted_local():
            return True
        session = self._session()
        if session is None:
            self._send_json({"error": "authentication required"}, 401)
            return False
        if csrf and self.headers.get("X-CSRF-Token", "") != session.csrf:
            self._send_json({"error": "invalid CSRF token"}, 403)
            return False
        return True

    def _read_json(self, max_bytes=2 * 1024 * 1024):
        length = int(self.headers.get("Content-Length", 0))
        if length > max_bytes:
            raise ValueError("request body too large")
        body = self.rfile.read(length).decode("utf-8") if length else ""
        return json.loads(body) if body else {}

    def _auth_session(self):
        session = self._session()
        self._send_json({
            "authenticated": self._is_admin(),
            "trusted_local": self._trusted_local(),
            "csrf_token": session.csrf if session else None,
        })

    def _device(self):
        from .auth import AuthManager
        from .store import Store
        auth = self.headers.get("Authorization")
        if not auth or not auth.startswith("Bearer "):
            return None
        with Store(str(self.db_path), backup_on_migrate=False) as store:
            row = store.device_for_token(AuthManager.token_hash(auth[7:].strip()))
            if row:
                store.conn.execute("UPDATE sync_devices SET last_seen=? WHERE device_id=?",
                                   (time.time(), row["device_id"]))
                store.conn.commit()
                return dict(row)
        return None

    def _require_device(self):
        device = self._device()
        if device is None:
            self._send_json({"error": "valid device token required"}, 401)
        return device

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/app/health":
                from .store import SCHEMA_VERSION
                self._send_json({"status": "ok", "schema_version": SCHEMA_VERSION,
                                 "app_mode": bool(self.app_token), "capabilities": [
                                     "tasks", "paper_detail", "pdf_range", "batch_actions"]})
                return
            if self.app_token and not self._app_authenticated():
                self._send_json({"error": "valid app token required"}, 401)
                return
            object_match = re.fullmatch(r"/api/sync/objects/([0-9a-f]{64})", parsed.path)
            if object_match:
                if not self._require_device():
                    return
                from .store import Store
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    row = store.conn.execute("SELECT * FROM stored_objects WHERE sha256=? AND deleted_at IS NULL",
                                             (object_match.group(1),)).fetchone()
                if not row or not Path(row["storage_path"]).is_file():
                    self._send_json({"error": "object not found"}, 404)
                    return
                self._send(Path(row["storage_path"]).read_bytes(), row["content_type"])
            elif parsed.path == "/robots.txt":
                self._send(b"User-agent: *\nDisallow: /\n", "text/plain; charset=utf-8")
            elif parsed.path == "/api/auth/session":
                self._auth_session()
            elif parsed.path == "/api/sync/handshake":
                from .protocol import handshake
                self._send_json(handshake())
            elif parsed.path == "/api/sync/pull":
                device = self._require_device()
                if not device:
                    return
                from .incremental import change_dict
                from .store import Store
                after = max(0, int(parse_qs(parsed.query).get("after", ["0"])[0]))
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    changes = [change_dict(row) for row in store.changes_after(after)]
                    latest = int(changes[-1]["server_sequence"]) if changes else after
                    server_latest = int(store.conn.execute(
                        "SELECT COALESCE(max(server_sequence),0) FROM sync_changes").fetchone()[0])
                    store.conn.execute(
                        """INSERT INTO sync_cursors(device_id,last_server_sequence,updated_at) VALUES (?,?,?)
                           ON CONFLICT(device_id) DO UPDATE SET last_server_sequence=max(last_server_sequence,excluded.last_server_sequence),
                           updated_at=excluded.updated_at""", (device["device_id"], latest, time.time())
                    )
                    store.conn.commit()
                self._send_json({"changes": changes, "latest_sequence": latest,
                                 "has_more": latest < server_latest})
            elif parsed.path == "/api/sync/status":
                if not self._require_admin(csrf=False):
                    return
                from .store import Store
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    runtime = store.conn.execute("SELECT * FROM sync_runtime WHERE id=1").fetchone()
                    self._send_json({
                        "devices": [dict(r) | {"token_hash": None} for r in store.conn.execute(
                            "SELECT * FROM sync_devices ORDER BY created_at")],
                        "open_conflicts": store.conn.execute(
                            "SELECT count(*) FROM sync_conflicts WHERE status='open'").fetchone()[0],
                        "latest_sequence": store.conn.execute(
                            "SELECT COALESCE(max(server_sequence),0) FROM sync_changes").fetchone()[0],
                        "pending_uploads": store.conn.execute(
                            "SELECT count(*) FROM sync_changes WHERE uploaded_at IS NULL").fetchone()[0],
                        "last_success": runtime["last_success"] if runtime else None,
                        "last_error": runtime["last_error"] if runtime else None,
                    })
            elif parsed.path == "/api/sync/conflicts":
                if not self._require_admin(csrf=False):
                    return
                from .store import Store
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    rows = [dict(r) for r in store.conn.execute(
                        "SELECT * FROM sync_conflicts WHERE status='open' ORDER BY created_at,id")]
                self._send_json({"conflicts": rows})
            elif re.fullmatch(r"/api/history/(note|review)/(.+)", parsed.path):
                if not self._require_admin(csrf=False):
                    return
                import urllib.parse as ul
                match = re.fullmatch(r"/api/history/(note|review)/(.+)", parsed.path)
                entity_type, entity_id = match.group(1), ul.unquote(match.group(2))
                from .store import Store
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    if entity_type == "review":
                        topic = store.get_collection_topic(int(entity_id))
                        if not topic:
                            self._send_json({"error": "topic not found"}, 404)
                            return
                        entity_id = topic["sync_id"]
                    rows = [dict(r) for r in store.conn.execute(
                        """SELECT version,content,content_hash,created_at,device_id FROM content_versions
                           WHERE entity_type=? AND entity_id=? ORDER BY version DESC""",
                        (entity_type, entity_id),
                    )]
                self._send_json({"versions": rows})
            elif parsed.path.startswith("/pdf/"):
                import urllib.parse as ul
                pid = ul.unquote(parsed.path[5:])
                self._serve_pdf(pid)
            elif parsed.path.startswith("/reports/"):
                self._serve_static_file(parsed)
            elif parsed.path == "/api/papers":
                self._send(json.dumps(query_papers(self.db_path, parse_qs(parsed.query)), ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            elif re.fullmatch(r"/api/papers/[^/]+/(reading-note|note)", parsed.path):
                if not self._require_admin(csrf=False):
                    return
                import urllib.parse as ul
                from .config import load_config
                from .reader import load_note
                from .store import Store
                suffix = "/reading-note" if parsed.path.endswith("/reading-note") else "/note"
                paper_id = ul.unquote(parsed.path[len("/api/papers/"):-len(suffix)])
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    exists = store.get(paper_id)
                    note = store.get_note(paper_id) if exists else None
                if not exists:
                    self._send_json({"error": "paper not found"}, 404)
                    return
                cfg = load_config(str(self.config_path))
                self._send_json({"paper_id": paper_id, "content": load_note(cfg.pdf_root, paper_id),
                                 "version": int(note["version"]) if note else 0,
                                 "updated_at": note["updated_at"] if note else None})
            elif re.fullmatch(r"/api/desktop/papers/[^/]+/note-path", parsed.path):
                if not self.app_token:
                    self._send_json({"error": "desktop only"}, 404); return
                import urllib.parse as ul
                from .config import load_config
                from .reader import _note_path
                paper_id = ul.unquote(parsed.path[len("/api/desktop/papers/"):-len("/note-path")])
                path = _note_path(load_config(str(self.config_path)).pdf_root, paper_id)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch(exist_ok=True)
                self._send_json({"path": str(path)})
            elif re.fullmatch(r"/api/papers/[^/]+/summary", parsed.path):
                if not self._require_admin(csrf=False):
                    return
                import urllib.parse as ul
                from .store import Store
                from .summary import public_summary
                paper_id = ul.unquote(parsed.path[len("/api/papers/"):-len("/summary")])
                try:
                    with Store(str(self.db_path), backup_on_migrate=False) as store:
                        self._send_json(public_summary(store, paper_id))
                except KeyError:
                    self._send_json({"error": "paper not found"}, 404)
            elif re.fullmatch(r"/api/papers/[^/]+", parsed.path):
                import urllib.parse as ul
                from .store import Store
                paper_id = ul.unquote(parsed.path[len("/api/papers/"):])
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    row = store.get(paper_id)
                self._send_json(_paper_dict(row) if row else {"error": "paper not found"}, 200 if row else 404)
            elif parsed.path == "/api/facets":
                self._send(json.dumps(query_facets(self.db_path), ensure_ascii=False).encode(),
                          "application/json; charset=utf-8")
            elif parsed.path == "/api/llm/config":
                if not self._require_admin(csrf=False):
                    return
                from .llm import public_provider_config
                self._send(json.dumps(public_provider_config(self.db_path), ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            elif parsed.path == "/api/llm/models":
                if not self._require_admin(csrf=False):
                    return
                from .llm import list_models
                provider = parse_qs(parsed.query).get("provider", [""])[0]
                try:
                    self._send_json({"provider": provider, "models": list_models(provider, self.db_path)})
                except Exception as exc:
                    self._send_json({"error": str(exc)}, 502)
            elif parsed.path == "/api/settings":
                if not self._require_admin(csrf=False):
                    return
                from .app_config import read_settings
                self._send_json(read_settings(self.config_path))
            elif parsed.path == "/api/activity":
                if not self._require_admin(csrf=False):
                    return
                params = parse_qs(parsed.query)
                actions = [item for value in params.get("actions", []) for item in value.split(",") if item]
                from .store import Store
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    items = store.recent_activity(
                        limit=int(params.get("limit", ["50"])[0]),
                        offset=int(params.get("offset", ["0"])[0]), actions=actions or None,
                        unique_papers=params.get("unique", ["0"])[0] in {"1", "true"},
                    )
                self._send_json({"activity": items})
            elif parsed.path == "/api/search":
                params = parse_qs(parsed.query)
                results = search_papers(self.db_path, params)
                self._send(json.dumps({"matches": results, "total": len(results)}, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            elif re.fullmatch(r"/api/papers/[^/]+/related", parsed.path):
                if not self._require_admin(csrf=False):
                    return
                import urllib.parse as ul
                from .config import load_config
                from .related import find_related
                paper_id = ul.unquote(parsed.path[len("/api/papers/"):-len("/related")])
                try:
                    self._send_json(find_related(self.db_path, load_config(str(self.config_path)), paper_id))
                except LookupError as exc:
                    self._send_json({"error": str(exc)}, 404)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, 502)
            elif parsed.path == "/api/topics":
                self._send_json(self._list_topics())
            elif parsed.path == "/api/tasks":
                from .app_tasks import TASKS
                self._send_json({"tasks": TASKS.list()})
            elif re.fullmatch(r"/api/tasks/[0-9a-f-]+", parsed.path):
                from .app_tasks import TASKS
                task = TASKS.get(parsed.path.rsplit("/", 1)[-1])
                self._send_json(task or {"error": "task not found"}, 200 if task else 404)
            elif self._topic_route(parsed.path, "papers"):
                topic_id = self._topic_route(parsed.path, "papers")
                result = query_collection_papers(self.db_path, topic_id)
                self._send_json(result or {"error": "topic not found"}, 200 if result else 404)
            elif self._topic_route(parsed.path, "review"):
                self._serve_topic_review(self._topic_route(parsed.path, "review"))
            elif self._topic_route(parsed.path):
                self._serve_topic(self._topic_route(parsed.path))
            elif parsed.path == "/api/report":
                self._send_json({"error": "deprecated", "replacement": "paper note and summary"}, 410)
            else:
                self._send_json({"error": "not found"}, 404)
        except Exception as exc:
            print("[web] GET error:", repr(exc), flush=True)
            self._send_json({"error": "internal server error"}, 500)

    def do_HEAD(self):
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/api/sync/objects/([0-9a-f]{64})", parsed.path)
        if not match:
            self._send(b"", "text/plain", 404)
            return
        if not self._require_device():
            return
        from .store import Store
        with Store(str(self.db_path), backup_on_migrate=False) as store:
            row = store.conn.execute("SELECT * FROM stored_objects WHERE sha256=? AND deleted_at IS NULL",
                                     (match.group(1),)).fetchone()
        if not row or not Path(row["storage_path"]).is_file():
            part = Path(self.db_path).resolve().parent / "objects" / (match.group(1) + ".part")
            self._send(b"", "application/octet-stream", 404,
                       {"X-Upload-Offset": str(part.stat().st_size if part.exists() else 0)})
            return
        self._send(b"", row["content_type"], 200, {"X-Object-Size": str(row["size"])})

    def _send_json(self, data, status=200):
        self._send(json.dumps(data, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", status)

    @staticmethod
    def _topic_route(path, suffix=None):
        pattern = r"/api/topics/(\d+)" + (r"/" + suffix if suffix else "") + r"/?$"
        match = re.fullmatch(pattern, path)
        return int(match.group(1)) if match else None

    @staticmethod
    def _topic_paper_route(path):
        match = re.fullmatch(r"/api/topics/(\d+)/papers/(.+)", path)
        if not match:
            return None
        import urllib.parse as ul
        return int(match.group(1)), ul.unquote(match.group(2))

    def _list_topics(self):
        from .store import Store
        with Store(str(self.db_path), backup_on_migrate=False) as store:
            return {"topics": [dict(r) for r in store.list_collection_topics()]}

    def _serve_topic(self, topic_id):
        from .store import Store
        with Store(str(self.db_path), backup_on_migrate=False) as store:
            row = store.get_collection_topic(topic_id)
        self._send_json(dict(row) if row else {"error": "topic not found"}, 200 if row else 404)

    def _serve_topic_review(self, topic_id):
        from .store import Store
        with Store(str(self.db_path), backup_on_migrate=False) as store:
            topic = store.get_collection_topic(topic_id)
            review = store.get_topic_review(topic_id) if topic else None
        if topic is None:
            self._send_json({"error": "topic not found"}, 404)
            return
        result = dict(review) if review else {"topic_id": topic_id, "status": "none"}
        path = result.get("review_path")
        if path and os.path.isfile(path):
            result["content"] = Path(path).read_text(encoding="utf-8")
        result.pop("review_path", None)
        if not self._is_admin() and result.get("error"):
            result["error"] = "review generation failed"
        self._send_json(result)

    def _run_topic_review(self, topic_id, provider, model):
        key = (str(Path(self.db_path).resolve()), topic_id)
        try:
            from .config import load_config
            from .store import Store
            from . import report as report_mod
            cfg = load_config(str(self.config_path))
            with Store(str(self.db_path), backup_on_migrate=False) as store:
                try:
                    report_mod.generate_topic_review(
                        store, cfg, topic_id, provider=provider, model=model, log=lambda m: None)
                except Exception as exc:
                    if store.get_collection_topic(topic_id):
                        current = store.get_topic_review(topic_id)
                        store.set_topic_review(
                            topic_id, status="error", stage="failed",
                            completed_papers=current["completed_papers"] if current else 0,
                            total_papers=current["total_papers"] if current else 0,
                            error=str(exc)[:2000])
        finally:
            with _TOPIC_TASKS_LOCK:
                _TOPIC_TASKS.discard(key)

    def _start_topic_review(self, topic_id, data):
        from .store import Store
        with Store(str(self.db_path), backup_on_migrate=False) as store:
            topic = store.get_collection_topic(topic_id)
            if topic is None:
                self._send_json({"error": "topic not found"}, 404)
                return
            papers = store.collection_papers(topic_id)
            if not papers:
                self._send_json({"error": "topic has no papers"}, 400)
                return
        key = (str(Path(self.db_path).resolve()), topic_id)
        with _TOPIC_TASKS_LOCK:
            if key in _TOPIC_TASKS:
                self._serve_topic_review(topic_id)
                return
            _TOPIC_TASKS.add(key)
        thread = threading.Thread(
            target=self._run_topic_review,
            args=(topic_id, data.get("provider"), data.get("model")), daemon=True)
        thread.start()
        self._send_json({"status": "running", "topic_id": topic_id}, 202)

    def _serve_pdf(self, pid):
        conn = _connect(self.db_path)
        try:
            row = conn.execute("SELECT pdf_path FROM papers WHERE id=?", (pid,)).fetchone()
        finally:
            conn.close()
        if not row or not row["pdf_path"]:
            self._send(b"PDF not downloaded", "text/plain; charset=utf-8", 404)
            return
        pdf = Path(row["pdf_path"])
        if not pdf.is_file() or pdf.stat().st_size < 1000:
            self._send(b"PDF file missing", "text/plain; charset=utf-8", 404)
            return
        size = pdf.stat().st_size
        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range", "")
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
        if match:
            start = int(match.group(1) or 0)
            end = min(size - 1, int(match.group(2) or size - 1))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Disposition", 'inline; filename="' + pdf.name + '"')
        if self.app_token:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization,Range")
        self.end_headers()
        with pdf.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(1 << 16, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _serve_static_file(self, parsed_path):
        import urllib.parse as ul
        rel = ul.unquote(parsed_path.path[len("/reports/"):])
        if ".." in rel:
            self._send(b"forbidden", "text/plain", 403)
            return
        from .config import load_config
        cfg = load_config(str(self.config_path))
        report_root = (cfg.pdf_root / "reports").resolve()
        file_path = (report_root / rel).resolve()
        if not file_path.is_relative_to(report_root):
            self._send(b"forbidden", "text/plain", 403)
            return
        if not file_path.is_file():
            self._send(b"not found", "text/plain; charset=utf-8", 404)
            return
        ext = file_path.suffix.lower()
        ct = {".md": "text/markdown; charset=utf-8", ".png": "image/png",
              ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
              ".svg": "image/svg+xml"}.get(ext, "application/octet-stream")
        data = file_path.read_bytes()
        extra = {"Content-Security-Policy": "sandbox"} if ext == ".svg" else None
        self._send(data, ct, headers=extra)

    def _serve_report(self, paper_id):
        conn = _connect(self.db_path)
        try:
            row = conn.execute("SELECT pdf_path FROM papers WHERE id=?", (paper_id,)).fetchone()
            rpt = conn.execute("SELECT report_path,status,generated_at,version FROM paper_reports WHERE paper_id=?", (paper_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            self._send(json.dumps({"error": "paper not found"}).encode(), "application/json", 404)
            return
        if rpt and rpt["report_path"] and os.path.isfile(rpt["report_path"]):
            content = Path(rpt["report_path"]).read_text(encoding="utf-8")
            self._send(json.dumps({"status": "ok", "content": content,
                                  "version": rpt["version"] if "version" in rpt.keys() else 1}).encode(),
                       "application/json; charset=utf-8")
            return
        if not row["pdf_path"] or not os.path.isfile(row["pdf_path"]):
            self._send(json.dumps({"error": "PDF not downloaded; report requires the PDF"}).encode(), "application/json", 400)
            return
        self._send(json.dumps({"status": "none"}).encode(), "application/json; charset=utf-8")

    def _do_report(self, paper_id, provider=None, model=None):
        conn = _connect(self.db_path)
        try:
            row = conn.execute("SELECT pdf_path FROM papers WHERE id=?", (paper_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            self._send(json.dumps({"error": "paper not found"}).encode(), "application/json", 404)
            return
        if not row["pdf_path"] or not os.path.isfile(row["pdf_path"]):
            self._send(json.dumps({"error": "PDF not downloaded; report requires the PDF"}).encode(), "application/json", 400)
            return
        try:
            from .config import load_config
            from .store import Store
            from . import report as report_mod
            cfg = load_config(str(self.config_path))
            with Store(str(self.db_path)) as store:
                store.record_activity(paper_id, "report_started")
                path = report_mod.generate_report(
                    store, cfg, paper_id, provider=provider, model=model, log=lambda m: None)
                store.record_activity(paper_id, "report_completed", {"path": str(path)})
            self._send(json.dumps({"status": "ok", "path": path}).encode(), "application/json; charset=utf-8")
        except Exception as exc:
            self._send(json.dumps({"error": str(exc)}).encode(), "application/json", 500)

    def _do_download(self, pid):
        conn = _connect(self.db_path)
        try:
            row = conn.execute("SELECT id, pdf_path FROM papers WHERE id=?", (pid,)).fetchone()
        finally:
            conn.close()
        if not row:
            self._send(json.dumps({"error": "not found"}).encode(), "application/json", 404)
            return
        if row["pdf_path"] and os.path.isfile(row["pdf_path"]):
            self._send(json.dumps({"status": "ok", "url": "/pdf/" + pid}).encode(), "application/json")
            return
        try:
            from .config import load_config
            from .store import Store
            from . import pipeline
            cfg = load_config(str(self.config_path))
            with Store(str(self.db_path)) as store:
                n = pipeline.download_single(store, cfg, pid, log=lambda m: None)
            if n:
                self._send(json.dumps({"status": "ok", "url": "/pdf/" + pid}).encode(), "application/json")
            else:
                self._send(json.dumps({"status": "failed"}).encode(), "application/json")
        except Exception as exc:
            self._send(json.dumps({"error": str(exc)}).encode(), "application/json", 500)

    def _save_markdown(self, entity_type, entity_id, content, base_version):
        import hashlib
        from .config import load_config
        from .store import Store
        from . import report as report_mod
        if not isinstance(content, str) or len(content.encode("utf-8")) > 2 * 1024 * 1024:
            self._send_json({"error": "invalid Markdown content"}, 400)
            return
        cfg = load_config(str(self.config_path))
        with Store(str(self.db_path), backup_on_migrate=False) as store:
            if entity_type == "report":
                paper = store.get(entity_id)
                if paper is None:
                    self._send_json({"error": "paper not found"}, 404)
                    return
                current_row = store.get_report(entity_id)
                current = int(current_row["version"] or 1) if current_row else 0
                path = Path(current_row["report_path"]) if current_row else report_mod.report_path_for(cfg, entity_id)
            else:
                topic = store.get_collection_topic(int(entity_id))
                if topic is None:
                    self._send_json({"error": "topic not found"}, 404)
                    return
                current_row = store.get_topic_review(int(entity_id))
                current = int(current_row["version"] or 1) if current_row else 0
                path = (Path(current_row["review_path"]) if current_row and current_row["review_path"]
                        else report_mod.topic_review_path_for(cfg, int(entity_id)))
                sync_entity_id = topic["sync_id"]
            if entity_type == "report":
                sync_entity_id = str(entity_id)
            if int(base_version or 0) != current:
                self._send_json({"error": "version conflict", "current_version": current}, 409)
                return
            new_version = current + 1
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(path.suffix + ".tmp")
            temp.write_text(content, encoding="utf-8")
            os.replace(temp, path)
            digest = hashlib.sha256(content.encode()).hexdigest()
            device = os.environ.get("PAPERLAB_DEVICE_ID", "local")
            objects = store.markdown_objects(content, path)
            store.conn.execute(
                """INSERT INTO content_versions(entity_type,entity_id,version,content,content_hash,created_at,device_id)
                   VALUES (?,?,?,?,?,?,?)""",
                (entity_type, sync_entity_id, new_version, content, digest, time.time(), device),
            )
            if entity_type == "report":
                now = time.time()
                store.conn.execute(
                    """INSERT INTO paper_reports(paper_id,report_path,status,generated_at,updated_at,version,updated_by_device)
                       VALUES (?,?,?,?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET
                       report_path=excluded.report_path,status='ok',updated_at=excluded.updated_at,
                       version=excluded.version,updated_by_device=excluded.updated_by_device""",
                    (entity_id, str(path), "ok", now, now, new_version, device),
                )
            else:
                now = time.time()
                store.conn.execute(
                    """INSERT INTO topic_reviews(topic_id,review_path,status,stage,updated_at,version,updated_by_device)
                       VALUES (?,?,?,'edited',?,?,?) ON CONFLICT(topic_id) DO UPDATE SET
                       review_path=excluded.review_path,status='ok',stage='edited',updated_at=excluded.updated_at,
                       version=excluded.version,updated_by_device=excluded.updated_by_device""",
                    (int(entity_id), str(path), "ok", now, new_version, device),
                )
            store.record_change(entity_type, sync_entity_id, "update",
                                {"content": content, "content_hash": digest, "objects": objects},
                                base_version=current, version=new_version)
            store.conn.commit()
        self._send_json({"status": "ok", "version": new_version})

    def do_PATCH(self):
        parsed = urlparse(self.path)
        try:
            if not self._require_admin():
                return
            data = self._read_json()
            paper_match = re.fullmatch(r"/api/papers/(.+)", parsed.path)
            if paper_match and "/" not in paper_match.group(1):
                import urllib.parse as ul
                try:
                    result = update_paper(self.db_path, ul.unquote(paper_match.group(1)),
                                          data.get("fields", data), data.get("base_version"))
                except RuntimeError:
                    self._send_json({"error": "version conflict"}, 409)
                    return
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, 409)
                    return
                self._send_json(result)
                return
            topic_id = self._topic_route(parsed.path)
            if topic_id:
                from .store import Store
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    try:
                        topic = store.update_topic_name(topic_id, data.get("name", ""),
                                                        base_version=data.get("base_version"))
                    except KeyError:
                        self._send_json({"error": "topic not found"}, 404)
                        return
                    except RuntimeError:
                        self._send_json({"error": "version conflict"}, 409)
                        return
                self._send_json(dict(topic))
                return
            self._send_json({"error": "not found"}, 404)
        except Exception as exc:
            print("[web] PATCH error:", repr(exc), flush=True)
            self._send_json({"error": "internal server error"}, 500)

    def do_PUT(self):
        parsed = urlparse(self.path)
        try:
            note_match = re.fullmatch(r"/api/papers/([^/]+)/note", parsed.path)
            if note_match:
                if not self._require_admin():
                    return
                data = self._read_json(5 * 1024 * 1024 + 4096)
                import urllib.parse as ul
                from .config import load_config
                from .store import Store
                paper_id = ul.unquote(note_match.group(1))
                content = data.get("content")
                if not isinstance(content, str):
                    self._send_json({"error": "content must be a string"}, 400); return
                if len(content.encode("utf-8")) > 5 * 1024 * 1024:
                    self._send_json({"error": "note exceeds 5 MB sync limit"}, 413); return
                cfg = load_config(str(self.config_path))
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    if not store.get(paper_id):
                        self._send_json({"error": "paper not found"}, 404); return
                    try:
                        note = store.save_note_content(cfg.pdf_root, paper_id, content,
                                                       base_version=data.get("base_version"))
                    except RuntimeError:
                        self._send_json({"error": "version conflict"}, 409); return
                    path = Path(note["note_path"])
                    store.record_activity(paper_id, "note_updated", dedupe_seconds=300)
                self._send_json({"status": "ok", "paper_id": paper_id,
                                 "version": int(note["version"]), "path": str(path)})
                return
            object_match = re.fullmatch(r"/api/sync/objects/([0-9a-f]{64})", parsed.path)
            if object_match:
                if not self._require_device():
                    return
                import hashlib
                from .store import Store
                digest = object_match.group(1)
                length = int(self.headers.get("Content-Length", 0))
                if length < 0 or length > 4 * 1024 * 1024:
                    self._send_json({"error": "invalid chunk size"}, 400)
                    return
                offset = int(self.headers.get("X-Upload-Offset", "0"))
                total = int(self.headers.get("X-Upload-Total", "0"))
                root = Path(self.db_path).resolve().parent / "objects"
                root.mkdir(parents=True, exist_ok=True)
                target = root / digest
                part = root / (digest + ".part")
                current = part.stat().st_size if part.exists() else 0
                if offset != current:
                    self._send_json({"error": "upload offset mismatch", "received": current}, 409)
                    return
                with part.open("ab") as handle:
                    handle.write(self.rfile.read(length))
                received = part.stat().st_size
                complete = bool(total and received == total)
                if received > total > 0:
                    part.unlink(missing_ok=True)
                    self._send_json({"error": "upload exceeds declared size"}, 400)
                    return
                if complete:
                    actual = hashlib.sha256(part.read_bytes()).hexdigest()
                    if actual != digest:
                        part.unlink(missing_ok=True)
                        self._send_json({"error": "object hash mismatch"}, 400)
                        return
                    os.replace(part, target)
                    with Store(str(self.db_path), backup_on_migrate=False) as store:
                        store.conn.execute(
                            """INSERT INTO stored_objects(sha256,size,content_type,storage_path,created_at)
                               VALUES (?,?,?,?,?) ON CONFLICT(sha256) DO UPDATE SET storage_path=excluded.storage_path,
                               size=excluded.size,deleted_at=NULL""",
                            (digest, total, self.headers.get("Content-Type", "application/octet-stream"),
                             str(target), time.time()),
                        )
                        store.conn.commit()
                self._send_json({"received": received, "complete": complete})
                return
            if not self._require_admin():
                return
            data = self._read_json()
            match = re.fullmatch(r"/api/reports/(.+)", parsed.path)
            if match:
                self._send_json({"error": "deprecated", "replacement": "paper note"}, 410)
                return
            topic_id = self._topic_route(parsed.path, "review")
            if topic_id:
                self._save_markdown("review", str(topic_id), data.get("content"), data.get("base_version"))
                return
            self._send_json({"error": "not found"}, 404)
        except Exception as exc:
            print("[web] PUT error:", repr(exc), flush=True)
            self._send_json({"error": "internal server error"}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if self.app_token and not self._app_authenticated():
                self._send_json({"error": "valid app token required"}, 401)
                return
            if parsed.path == "/api/papers/import/preview":
                if not self._require_admin():
                    return
                from .paper_import import ImportError as PaperImportError, MAX_PDF_BYTES, preview_pdf
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/pdf":
                    self._send_json({"error": "Content-Type must be application/pdf"}, 415)
                    return
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0:
                    self._send_json({"error": "empty PDF upload"}, 400)
                    return
                if length > MAX_PDF_BYTES:
                    self._send_json({"error": "PDF exceeds 50 MB limit"}, 413)
                    return
                try:
                    result = preview_pdf(
                        self.db_path, self.rfile.read(length),
                        provider=self.headers.get("X-PaperLab-Provider"),
                        model=self.headers.get("X-PaperLab-Model"),
                    )
                except PaperImportError as exc:
                    self._send_json({"error": str(exc)}, 400)
                    return
                self._send_json(result)
                return
            pdf_match = re.fullmatch(r"/api/papers/(.+)/pdf", parsed.path)
            if pdf_match:
                if not self._require_admin():
                    return
                import urllib.parse as ul
                from .paper_import import MAX_PDF_BYTES
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > MAX_PDF_BYTES:
                    self._send_json({"error": "PDF must be between 1 byte and 50 MB"}, 413)
                    return
                content = self.rfile.read(length)
                if not content.startswith(b"%PDF-"):
                    self._send_json({"error": "file is not a PDF"}, 415)
                    return
                try:
                    import pymupdf
                    document = pymupdf.open(stream=content, filetype="pdf")
                    if document.page_count < 1:
                        raise ValueError("PDF has no pages")
                    document.close()
                except Exception as exc:
                    self._send_json({"error": "invalid PDF: " + str(exc)}, 400)
                    return
                from .config import load_config
                from .store import Store
                paper_id = ul.unquote(pdf_match.group(1))
                cfg = load_config(str(self.config_path))
                target_dir = cfg.pdf_root / "pdfs"
                target_dir.mkdir(parents=True, exist_ok=True)
                safe = re.sub(r"[^A-Za-z0-9._-]+", "_", paper_id)[:150]
                target = target_dir / (safe + ".pdf")
                temp = target.with_suffix(".pdf.tmp")
                temp.write_bytes(content)
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    if not store.get(paper_id):
                        temp.unlink(missing_ok=True)
                        self._send_json({"error": "paper not found"}, 404)
                        return
                    os.replace(temp, target)
                    store.set_pdf(paper_id, path=str(target), status="ok")
                    store.record_activity(paper_id, "pdf_attached", {"size": len(content)})
                self._send_json({"status": "ok", "paper_id": paper_id, "pdf_path": str(target)})
                return
            data = self._read_json(5 * 1024 * 1024 if parsed.path == "/api/sync/push" else 2 * 1024 * 1024)
            if parsed.path == "/api/activity":
                from .store import Store
                paper_id = str(data.get("paper_id") or "")
                action = str(data.get("action") or "viewed")
                allowed = {"viewed", "reading_started", "imported", "merged", "metadata_edited",
                           "pdf_attached", "report_started", "report_completed", "note_updated"}
                if action not in allowed:
                    self._send_json({"error": "unsupported activity"}, 400)
                    return
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    if not store.get(paper_id):
                        self._send_json({"error": "paper not found"}, 404)
                        return
                    store.record_activity(paper_id, action, data.get("details"),
                                          dedupe_seconds=300 if action == "viewed" else 30)
                self._send_json({"status": "ok"})
                return
            pdf_url_match = re.fullmatch(r"/api/papers/(.+)/pdf-url", parsed.path)
            if pdf_url_match:
                import urllib.parse as ul
                from .config import load_config
                from .paper_import import _download_pdf_url
                from .store import Store
                paper_id = ul.unquote(pdf_url_match.group(1))
                try:
                    _token, downloaded, warnings = _download_pdf_url(self.db_path, str(data.get("pdf_url") or ""))
                    cfg = load_config(str(self.config_path))
                    target_dir = cfg.pdf_root / "pdfs"
                    target_dir.mkdir(parents=True, exist_ok=True)
                    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", paper_id)[:150]
                    target = target_dir / (safe + ".pdf")
                    os.replace(downloaded, target)
                    with Store(str(self.db_path), backup_on_migrate=False) as store:
                        if not store.get(paper_id):
                            target.unlink(missing_ok=True)
                            self._send_json({"error": "paper not found"}, 404)
                            return
                        store.set_pdf(paper_id, path=str(target), status="ok", pdf_url=str(data.get("pdf_url") or ""))
                        store.record_activity(paper_id, "pdf_attached", {"url": data.get("pdf_url")})
                    self._send_json({"status": "ok", "warnings": warnings})
                except Exception as exc:
                    self._send_json({"error": str(exc)}, 400)
                return
            if parsed.path == "/api/settings":
                from .app_config import save_settings
                try:
                    self._send_json(save_settings(self.config_path, data))
                except Exception as exc:
                    self._send_json({"error": str(exc)}, 400)
                return
            if parsed.path == "/api/settings/restore":
                from .app_config import restore_backup
                try:
                    self._send_json(restore_backup(self.config_path, str(data.get("backup") or "")))
                except Exception as exc:
                    self._send_json({"error": str(exc)}, 400)
                return
            if parsed.path == "/api/llm/test":
                from .llm import test_provider
                try:
                    self._send_json(test_provider(str(data.get("provider") or ""), self.db_path))
                except Exception as exc:
                    self._send_json({"error": str(exc)}, 502)
                return
            summary_match = re.fullmatch(r"/api/papers/([^/]+)/summary", parsed.path)
            if summary_match:
                if not self._require_admin():
                    return
                import urllib.parse as ul
                from .app_tasks import TASKS
                paper_id = ul.unquote(summary_match.group(1))
                def work(update, cancelled):
                    if cancelled():
                        return None
                    from .store import Store
                    from .summary import generate_summary
                    update(.2)
                    with Store(str(self.db_path), backup_on_migrate=False) as store:
                        result = generate_summary(store, paper_id)
                    update(.9)
                    return result
                self._send_json(TASKS.submit("generate_summary", "生成中文概要", work), 202)
                return
            reading_match = re.fullmatch(r"/api/papers/([^/]+)/(reading-note|note|ask)", parsed.path)
            if reading_match:
                if not self._require_admin():
                    return
                import urllib.parse as ul
                from .config import load_config
                from .store import Store
                paper_id = ul.unquote(reading_match.group(1))
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    row = store.get(paper_id)
                if not row:
                    self._send_json({"error": "paper not found"}, 404)
                    return
                cfg = load_config(str(self.config_path))
                if reading_match.group(2) in {"reading-note", "note"}:
                    content = data.get("content")
                    if not isinstance(content, str):
                        self._send_json({"error": "content must be a string"}, 400)
                        return
                    if len(content.encode("utf-8")) > 5 * 1024 * 1024:
                        self._send_json({"error": "note exceeds 5 MB sync limit"}, 413)
                        return
                    with Store(str(self.db_path), backup_on_migrate=False) as store:
                        try:
                            note = store.save_note_content(cfg.pdf_root, paper_id, content,
                                                           base_version=data.get("base_version"))
                            path = Path(note["note_path"])
                        except RuntimeError:
                            self._send_json({"error": "version conflict"}, 409)
                            return
                        except ValueError as exc:
                            self._send_json({"error": str(exc)}, 413)
                            return
                        store.record_activity(paper_id, "note_updated", dedupe_seconds=300)
                    self._send_json({"status": "ok", "paper_id": paper_id,
                                     "version": int(note["version"]), "path": str(path)})
                    return
                from .reader import ask_pdf
                try:
                    result = ask_pdf(self.db_path, dict(row), str(data.get("question") or ""),
                                     data.get("history") if isinstance(data.get("history"), list) else [])
                    self._send_json(result)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, 400)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, 502)
                return
            if parsed.path == "/api/tasks":
                if not self._require_admin():
                    return
                from .app_tasks import TASKS
                from .config import load_config
                kind = str(data.get("kind") or "")
                paper_id = str(data.get("paper_id") or "")
                cfg = load_config(str(self.config_path))
                if kind == "download_pdf":
                    def work(update, cancelled):
                        if cancelled():
                            return None
                        from .store import Store
                        from . import pipeline
                        update(.1)
                        with Store(str(self.db_path)) as store:
                            count = pipeline.download_single(store, cfg, paper_id, log=lambda _: update(.6))
                        return {"paper_id": paper_id, "downloaded": bool(count), "url": "/pdf/" + paper_id}
                    task = TASKS.submit(kind, "Download PDF", work)
                elif kind in {"generate_summary", "generate_report"}:
                    def work(update, cancelled):
                        if cancelled():
                            return None
                        from .store import Store
                        from .summary import generate_summary
                        update(.1)
                        with Store(str(self.db_path)) as store:
                            result = generate_summary(store, paper_id)
                        update(.9)
                        return result
                    task = TASKS.submit("generate_summary", "生成中文概要", work)
                elif kind == "related_search":
                    def work(update, cancelled):
                        if cancelled():
                            return None
                        from .related import find_related
                        update(.2)
                        return find_related(self.db_path, cfg, paper_id)
                    task = TASKS.submit(kind, "Find related papers", work)
                else:
                    self._send_json({"error": "unsupported task kind"}, 400)
                    return
                self._send_json(task, 202)
                return
            cancel_match = re.fullmatch(r"/api/tasks/([0-9a-f-]+)/cancel", parsed.path)
            if cancel_match:
                if not self._require_admin():
                    return
                from .app_tasks import TASKS
                task = TASKS.cancel(cancel_match.group(1))
                self._send_json(task or {"error": "task not found"}, 200 if task else 404)
                return
            if parsed.path == "/api/papers/batch":
                if not self._require_admin():
                    return
                ids = [str(item) for item in data.get("paper_ids") or []][:500]
                action = data.get("action")
                if action == "add_to_topic":
                    from .store import Store
                    topic_id = int(data.get("topic_id"))
                    changed, failed = 0, []
                    with Store(str(self.db_path), backup_on_migrate=False) as store:
                        for paper_id in ids:
                            try:
                                changed += int(store.add_paper_to_collection(topic_id, paper_id))
                            except Exception as exc:
                                failed.append({"paper_id": paper_id, "error": str(exc)})
                    self._send_json({"changed": changed, "failed": failed})
                    return
                self._send_json({"error": "unsupported batch action"}, 400)
                return
            if parsed.path == "/api/papers/import/url/preview":
                if not self._require_admin():
                    return
                from .paper_import import ImportError as PaperImportError, preview_pdf_url
                try:
                    result = preview_pdf_url(self.db_path, data.get("pdf_url", ""),
                                             provider=data.get("provider"), model=data.get("model"))
                except PaperImportError as exc:
                    self._send_json({"error": str(exc)}, 400)
                    return
                self._send_json(result)
                return
            if parsed.path == "/api/papers/import/bibtex/preview":
                if not self._require_admin():
                    return
                from .config import load_config
                from .paper_import import ImportError as PaperImportError, preview_bibtex
                try:
                    result = preview_bibtex(self.db_path, data.get("bibtex", ""),
                                             cfg=load_config(str(self.config_path)))
                except PaperImportError as exc:
                    self._send_json({"error": str(exc)}, 400)
                    return
                self._send_json(result)
                return
            if parsed.path == "/api/papers/import/batch":
                if not self._require_admin():
                    return
                from .config import load_config
                from .paper_import import finalize_batch
                cfg = load_config(str(self.config_path))
                self._send_json(finalize_batch(self.db_path, cfg.pdf_root, data.get("items") or []))
                return
            if parsed.path == "/api/auth/login":
                from .auth import AUTH, SESSION_COOKIE
                session = AUTH.login(str(data.get("username") or ""), str(data.get("password") or ""),
                                     self.client_address[0])
                if session is None:
                    print(f"[auth] failed login from {self.client_address[0]}", flush=True)
                    self._send_json({"error": "invalid credentials or rate limit exceeded"}, 401)
                    return
                secure = os.environ.get("PAPERLAB_COOKIE_SECURE", "1").lower() in {"1", "true", "yes"}
                cookie = f"{SESSION_COOKIE}={session.token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400"
                if secure:
                    cookie += "; Secure"
                self._send(json.dumps({"authenticated": True, "csrf_token": session.csrf}).encode(),
                           "application/json; charset=utf-8", headers={"Set-Cookie": cookie})
                return
            if parsed.path == "/api/auth/logout":
                if not self._require_admin():
                    return
                from .auth import AUTH, SESSION_COOKIE
                AUTH.logout(self.headers.get("Cookie"))
                self._send(b'{"authenticated":false}', "application/json; charset=utf-8",
                           headers={"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})
                return
            if parsed.path == "/api/sync/push":
                device = self._require_device()
                if not device:
                    return
                from .config import load_config
                from .incremental import push_to_server
                from .store import Store
                changes = data.get("changes") or []
                if not isinstance(changes, list) or len(changes) > 500:
                    self._send_json({"error": "invalid changes"}, 400)
                    return
                cfg = load_config(str(self.config_path))
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    result = push_to_server(store, changes, cfg)
                self._send_json(result)
                return
            if not self._require_admin():
                return
            if parsed.path == "/api/sync/devices":
                import secrets
                from .auth import AuthManager
                from .store import Store
                device_id = str(data.get("device_id") or secrets.token_hex(8))
                token = secrets.token_urlsafe(32)
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    store.register_device(device_id, str(data.get("name") or device_id),
                                          AuthManager.token_hash(token))
                self._send_json({"device_id": device_id, "token": token}, 201)
                return
            conflict_match = re.fullmatch(r"/api/sync/conflicts/(\d+)/resolve", parsed.path)
            if conflict_match:
                from .config import load_config
                from .incremental import apply_change
                from .store import Store
                strategy = data.get("strategy")
                if strategy not in {"local", "remote", "manual"}:
                    self._send_json({"error": "invalid strategy"}, 400)
                    return
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    conflict = store.conn.execute(
                        "SELECT * FROM sync_conflicts WHERE id=? AND status='open'",
                        (int(conflict_match.group(1)),),
                    ).fetchone()
                    if not conflict:
                        self._send_json({"error": "conflict not found"}, 404)
                        return
                    if strategy != "local":
                        value = data.get("value") if strategy == "manual" else json.loads(conflict["remote_value"])
                        version = max(conflict["local_version"], conflict["remote_version"]) + 1
                        change = {"entity_type": conflict["entity_type"], "entity_id": conflict["entity_id"],
                                  "operation": "update", "changed_fields": {conflict["field"]: value},
                                  "base_version": conflict["local_version"], "version": version,
                                  "device_id": os.environ.get("PAPERLAB_DEVICE_ID", "server")}
                        apply_change(store, change, load_config(str(self.config_path)), remote=False)
                        store.record_change(change["entity_type"], change["entity_id"], "update",
                                            change["changed_fields"], base_version=change["base_version"],
                                            version=version)
                    store.conn.execute("UPDATE sync_conflicts SET status='resolved',resolved_at=? WHERE id=?",
                                       (time.time(), conflict["id"]))
                    store.conn.commit()
                self._send_json({"status": "resolved"})
                return
            if parsed.path == "/api/download":
                self._do_download(data.get("paper_id", ""))
            elif parsed.path == "/api/papers":
                from .config import load_config
                from .paper_import import ImportError as PaperImportError, finalize_import
                try:
                    cfg = load_config(str(self.config_path))
                    result = finalize_import(self.db_path, cfg.pdf_root, data)
                except PaperImportError as exc:
                    self._send_json({"error": str(exc)}, 400)
                    return
                if result.get("conflict"):
                    self._send_json({"error": "paper already exists", **result}, 409)
                    return
                result_paper = result.get("paper") or {}
                activity_id = result_paper.get("id") or result.get("paper_id") or result.get("id")
                if activity_id:
                    from .store import Store
                    with Store(str(self.db_path), backup_on_migrate=False) as activity_store:
                        activity_store.record_activity(activity_id,
                                                       "imported" if result.get("created") else "merged")
                self._send_json(result, 201 if result.get("created") else 200)
            elif parsed.path == "/api/edit":
                try:
                    result = update_paper(self.db_path, data.get("paper_id", ""), data.get("fields", {}),
                                          data.get("base_version"))
                except RuntimeError:
                    self._send_json({"error": "version conflict"}, 409)
                    return
                self._send(json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            elif parsed.path == "/api/llm/config":
                from .llm import save_provider_config
                result = save_provider_config(self.db_path, data)
                self._send(json.dumps(result, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            elif parsed.path == "/api/open_file":
                path = data.get("path", "")
                if not path or ".." in path:
                    self._send(json.dumps({"error": "invalid path"}).encode(), "application/json", 400)
                else:
                    import subprocess
                    try:
                        subprocess.Popen(["open", path])
                        self._send(json.dumps({"status": "ok"}).encode(), "application/json")
                    except Exception as exc:
                        self._send(json.dumps({"error": str(exc)}).encode(), "application/json", 500)
            elif parsed.path == "/api/report":
                self._do_report(data.get("paper_id", ""), data.get("provider"), data.get("model"))
            elif parsed.path == "/api/topics":
                from .store import Store
                try:
                    with Store(str(self.db_path), backup_on_migrate=False) as store:
                        topic = store.create_collection_topic(data.get("name", ""))
                    self._send_json(dict(topic), 201)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, 400)
                except sqlite3.IntegrityError:
                    self._send_json({"error": "topic name already exists"}, 409)
            elif self._topic_route(parsed.path, "review"):
                self._start_topic_review(self._topic_route(parsed.path, "review"), data)
            elif self._topic_paper_route(parsed.path):
                topic_id, paper_id = self._topic_paper_route(parsed.path)
                from .store import Store
                try:
                    with Store(str(self.db_path), backup_on_migrate=False) as store:
                        changed = store.add_paper_to_collection(topic_id, paper_id)
                    self._send_json({"status": "ok", "added": changed})
                except KeyError:
                    self._send_json({"error": "topic not found"}, 404)
                except LookupError:
                    self._send_json({"error": "paper not found"}, 404)
            else:
                self._send(b"not found", "text/plain; charset=utf-8", 404)
        except Exception as exc:
            print("[web] POST error:", repr(exc), flush=True)
            self._send_json({"error": "internal server error"}, 500)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        try:
            if not self._require_admin():
                return
            pdf_match = re.fullmatch(r"/api/papers/(.+)/pdf", parsed.path)
            if pdf_match:
                import urllib.parse as ul
                from .config import load_config
                from .store import Store
                paper_id = ul.unquote(pdf_match.group(1))
                cfg = load_config(str(self.config_path))
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    paper = store.get(paper_id)
                    if not paper:
                        self._send_json({"error": "paper not found"}, 404)
                        return
                    old = Path(paper["pdf_path"]).resolve() if paper["pdf_path"] else None
                    store.set_pdf(paper_id, path=None, status="missing")
                    store.record_activity(paper_id, "pdf_attached", {"removed": True})
                if old and old.is_file() and old.is_relative_to(cfg.pdf_root.resolve()):
                    old.unlink()
                self._send_json({"status": "ok"})
                return
            device_match = re.fullmatch(r"/api/sync/devices/([^/]+)", parsed.path)
            if device_match:
                import urllib.parse as ul
                from .store import Store
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    result = store.conn.execute(
                        "UPDATE sync_devices SET revoked_at=? WHERE device_id=? AND revoked_at IS NULL",
                        (time.time(), ul.unquote(device_match.group(1))),
                    )
                    store.conn.commit()
                self._send_json({"revoked": bool(result.rowcount)})
                return
            paper_route = self._topic_paper_route(parsed.path)
            if paper_route:
                topic_id, paper_id = paper_route
                from .store import Store
                try:
                    with Store(str(self.db_path), backup_on_migrate=False) as store:
                        removed = store.remove_paper_from_collection(topic_id, paper_id)
                    self._send_json({"status": "ok", "removed": removed})
                except KeyError:
                    self._send_json({"error": "topic not found"}, 404)
            elif self._topic_route(parsed.path):
                topic_id = self._topic_route(parsed.path)
                from .store import Store
                with Store(str(self.db_path), backup_on_migrate=False) as store:
                    deleted = store.delete_collection_topic(topic_id)
                if deleted is None:
                    self._send_json({"error": "topic not found"}, 404)
                    return
                review = deleted["review"]
                review_path = review["review_path"] if review else None
                if review_path:
                    path = Path(review_path)
                    if path.is_file():
                        path.unlink()
                self._send_json({"status": "ok"})
            else:
                self._send_json({"error": "not found"}, 404)
        except Exception as exc:
            print("[web] DELETE error:", repr(exc), flush=True)
            self._send_json({"error": "internal server error"}, 500)

    def log_message(self, fmt, *args):
        print("[web] " + fmt % args, flush=True)


def serve(db_path, host="127.0.0.1", port=0, config_path=None, *, app_token):
    if host not in {"127.0.0.1", "::1"}:
        raise ValueError("the PaperLab App sidecar only listens on loopback")
    if not app_token:
        raise ValueError("the PaperLab App sidecar requires an app token")
    import socketserver as _ss
    _ss.TCPServer.allow_reuse_address = True
    handler = type("ConfiguredPaperHandler", (PaperHandler,), {
        "db_path": Path(db_path),
        "config_path": Path(config_path).resolve() if config_path else PaperHandler.config_path,
        "app_token": str(app_token or ""),
        "no_browser_ui": True,
    })
    server = ThreadingHTTPServer((host, port), handler)
    # Recovery belongs to startup, never to a public GET request.
    from .config import load_config
    cfg = load_config(str(handler.config_path))
    repaired_pdfs = reconcile_pdf_paths(db_path, cfg.pdf_root)
    if repaired_pdfs:
        print(f"PaperLab repaired {repaired_pdfs} PDF path(s)", flush=True)
    from .paper_import import cleanup_previews
    cleanup_previews(db_path)
    from .store import Store
    with Store(str(db_path), backup_on_migrate=False) as store:
        from .reader import reconcile_notes
        reconciled_notes = reconcile_notes(store, cfg.pdf_root)
        if reconciled_notes:
            print(f"PaperLab registered {reconciled_notes} Markdown note(s) for sync", flush=True)
        store.conn.execute(
            """UPDATE topic_reviews SET status='error',stage='interrupted',
               error='previous review generation was interrupted; retry generation',updated_at=?
               WHERE status='running'""", (time.time(),)
        )
        store.conn.commit()
        store.run_sync_maintenance()
    auto_sync = os.environ.get("PAPERLAB_AUTO_SYNC", "1").lower() not in {"0", "false", "no", "off"}
    from .transport import sync_configured
    if auto_sync and sync_configured():
        def sync_loop():
            from .config import load_config
            from .incremental import sync_now
            cfg = load_config(str(handler.config_path))
            while True:
                try:
                    sync_now(cfg, log=lambda message: print("[sync] " + message, flush=True))
                except Exception as exc:
                    print("[sync] " + str(exc), flush=True)
                    with Store(str(cfg.db_path), backup_on_migrate=False) as store:
                        store.conn.execute(
                            """INSERT INTO sync_runtime(id,last_error,last_attempt) VALUES (1,?,?)
                               ON CONFLICT(id) DO UPDATE SET last_error=excluded.last_error,
                               last_attempt=excluded.last_attempt""", (str(exc)[:1000], time.time())
                        )
                        store.conn.commit()
                time.sleep(60)
        threading.Thread(target=sync_loop, daemon=True).start()
    actual_port = int(server.server_address[1])
    print("PAPERLAB_READY " + json.dumps({"host": host, "port": actual_port,
                                           "app_mode": bool(app_token)}), flush=True)
    print("PaperLab Web: http://{}:{} ({})".format(host, actual_port, db_path), flush=True)
    import signal
    previous_sigterm = None
    if threading.current_thread() is threading.main_thread():
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        def terminate(signum, frame):
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        from .transport import close_sync_tunnels
        close_sync_tunnels()
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
