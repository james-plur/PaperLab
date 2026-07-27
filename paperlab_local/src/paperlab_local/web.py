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
    where = " AND ".join(clauses)
    conn = _connect(db_path)
    try:
        total = conn.execute("SELECT count(*) FROM papers p WHERE " + where, values).fetchone()[0]
        rows = conn.execute(
            "SELECT " + _paper_select("p") + ","
            "(SELECT group_concat(pt.topic_id || ':' || printf('%.2f',pt.score),'|') FROM paper_topics pt WHERE pt.paper_id=p.id AND pt.relevant=1) topics "
            "FROM papers p WHERE " + where + " ORDER BY p.year DESC,p.venue,p.title LIMIT ? OFFSET ?",
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
               "doi", "arxiv_id", "source_url", "pdf_url", "awards")
    from .store import Store
    with Store(str(db_path), backup_on_migrate=False) as store:
        conn = store.conn
        row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        if row is None:
            return {"updated": 0}
        current = int(row["version"] or 1)
        if base_version is not None and int(base_version) != current:
            raise RuntimeError("version conflict")
        sets = []
        vals = []
        changed = {}
        for k in allowed:
            if k in fields:
                sets.append(k + "=?")
                value = fields[k] if fields[k] else None
                vals.append(value)
                changed[k] = value
        if not sets:
            return {"updated": 0}
        sets.append("last_updated=?")
        vals.append(time.time())
        sets.extend(["version=?", "updated_by_device=?"])
        vals.extend([current + 1, os.environ.get("PAPERLAB_DEVICE_ID", "local")])
        vals.append(paper_id)
        conn.execute("UPDATE papers SET " + ",".join(sets) + " WHERE id=?", vals)
        store.record_change("paper", paper_id, "update", changed,
                            base_version=current, version=current + 1)
        conn.commit()
        row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        return {"updated": 1, "paper": dict(row) if row else None}


class PaperHandler(BaseHTTPRequestHandler):
    db_path = Path(".")
    config_path = Path(__file__).resolve().parent.parent / "topics.yaml"

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
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _trusted_local(self):
        enabled = os.environ.get("PAPERLAB_LOCAL_TRUSTED_MODE", "1").lower() in {"1", "true", "yes"}
        return enabled and self.client_address[0] in {"127.0.0.1", "::1"}

    def _session(self):
        from .auth import AUTH
        return AUTH.from_cookie(self.headers.get("Cookie"))

    def _is_admin(self):
        return self._trusted_local() or self._session() is not None

    def _require_admin(self, *, csrf=True):
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
            elif re.fullmatch(r"/api/history/(report|review)/(.+)", parsed.path):
                if not self._require_admin(csrf=False):
                    return
                import urllib.parse as ul
                match = re.fullmatch(r"/api/history/(report|review)/(.+)", parsed.path)
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
            elif parsed.path == "/api/facets":
                self._send(json.dumps(query_facets(self.db_path), ensure_ascii=False).encode(),
                          "application/json; charset=utf-8")
            elif parsed.path == "/api/llm/config":
                if not self._require_admin(csrf=False):
                    return
                from .llm import public_provider_config
                self._send(json.dumps(public_provider_config(self.db_path), ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            elif parsed.path == "/api/search":
                params = parse_qs(parsed.query)
                results = search_papers(self.db_path, params)
                self._send(json.dumps({"matches": results, "total": len(results)}, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            elif parsed.path == "/api/topics":
                self._send_json(self._list_topics())
            elif self._topic_route(parsed.path, "papers"):
                topic_id = self._topic_route(parsed.path, "papers")
                result = query_collection_papers(self.db_path, topic_id)
                self._send_json(result or {"error": "topic not found"}, 200 if result else 404)
            elif self._topic_route(parsed.path, "review"):
                self._serve_topic_review(self._topic_route(parsed.path, "review"))
            elif self._topic_route(parsed.path):
                self._serve_topic(self._topic_route(parsed.path))
            elif parsed.path == "/api/report":
                import urllib.parse as ul
                pid = ul.unquote(parse_qs(parsed.query).get("id", [""])[0])
                self._serve_report(pid)
            elif parsed.path in ("/", "/index.html", "/login"):
                self._send(HTML.encode(), "text/html; charset=utf-8")
            else:
                self._send(b"not found", "text/plain; charset=utf-8", 404)
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
            missing = []
            for paper in papers:
                report = store.get_report(paper["id"])
                path = Path(report["report_path"]) if report and report["report_path"] else None
                if not report or report["status"] != "ok" or not path or not path.is_file():
                    missing.append({"paper_id": paper["id"], "title": paper["title"]})
            if missing:
                self._send_json({
                    "error": "all papers must have reports before generating a review",
                    "missing_reports": missing,
                }, 409)
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
        data = pdf.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", 'inline; filename="' + pdf.name + '"')
        self.end_headers()
        self.wfile.write(data)

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
                path = report_mod.generate_report(
                    store, cfg, paper_id, provider=provider, model=model, log=lambda m: None)
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
                import urllib.parse as ul
                self._save_markdown("report", ul.unquote(match.group(1)), data.get("content"), data.get("base_version"))
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
            data = self._read_json(5 * 1024 * 1024 if parsed.path == "/api/sync/push" else 2 * 1024 * 1024)
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


def serve(db_path, host="127.0.0.1", port=8765, config_path=None):
    import socketserver as _ss
    _ss.TCPServer.allow_reuse_address = True
    handler = type("ConfiguredPaperHandler", (PaperHandler,), {
        "db_path": Path(db_path),
        "config_path": Path(config_path).resolve() if config_path else PaperHandler.config_path,
    })
    server = ThreadingHTTPServer((host, port), handler)
    # Recovery belongs to startup, never to a public GET request.
    from .config import load_config
    cfg = load_config(str(handler.config_path))
    repaired_pdfs = reconcile_pdf_paths(db_path, cfg.pdf_root)
    if repaired_pdfs:
        print(f"PaperLab repaired {repaired_pdfs} PDF path(s)", flush=True)
    from .store import Store
    with Store(str(db_path), backup_on_migrate=False) as store:
        store.conn.execute(
            """UPDATE topic_reviews SET status='error',stage='interrupted',
               error='previous review generation was interrupted; retry generation',updated_at=?
               WHERE status='running'""", (time.time(),)
        )
        store.conn.commit()
        store.run_sync_maintenance()
    if os.environ.get("PAPERLAB_SYNC_URL") and os.environ.get("PAPERLAB_DEVICE_TOKEN"):
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
    print("PaperLab Web: http://{}:{} ({})".format(host, port, db_path), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>PaperLab</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--ink:#18202a;--muted:#68717d;--line:#dfe3e8;--accent:#176b5b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,sans-serif}
header{position:sticky;top:0;z-index:3;background:#ffffffed;border-bottom:1px solid var(--line);padding:14px 20px;backdrop-filter:blur(8px)}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;max-width:1500px;margin:auto}h1{font-size:19px;margin:0 12px 0 0}
input,select,button,textarea{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:#fff}input{flex:1;min-width:200px}button{cursor:pointer}.stat{color:var(--muted)}
.layout{display:grid;grid-template-columns:240px minmax(0,1fr);max-width:1740px;margin:auto}.sidebar{position:sticky;top:69px;height:calc(100vh - 69px);padding:18px 12px;border-right:1px solid var(--line);background:#fff;overflow:auto}.sidebar-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}.sidebar h2{font-size:15px;margin:0}.topic-item{display:flex;align-items:center;gap:6px;padding:8px;border-radius:8px;cursor:pointer}.topic-item:hover,.topic-item.active{background:#edf2f1}.topic-name{flex:1;overflow:hidden;text-overflow:ellipsis}.topic-count{color:var(--muted);font-size:12px}.topic-delete{border:0;background:transparent;color:#9b3b3b;padding:2px 5px}.home-link{margin-bottom:10px;font-weight:600}main{max-width:1500px;width:100%;margin:16px auto;padding:0 20px}.paper{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0}
.title{font-size:16px;font-weight:650}.meta,.authors,.orgs{color:var(--muted);margin-top:5px}.abstract{margin:10px 0 0;max-width:1200px}
.chip{display:inline-block;background:#edf2f1;border-radius:12px;padding:2px 8px;margin-right:5px;font-size:12px}
.kw-chip{background:#e8f0fe;color:#1a5276;cursor:pointer}.kw-chip:hover{background:#d0e0fc}
a{color:var(--accent);text-decoration:none}.pager{display:flex;justify-content:center;align-items:center;gap:12px;margin:22px}.empty{text-align:center;color:var(--muted);padding:60px}
.link-btn{display:inline-block;background:#e9ecef;color:var(--ink);padding:4px 10px;border-radius:6px;font-size:13px;text-decoration:none;cursor:pointer;margin-left:6px}.link-btn:hover{background:#dee2e6}
.dl-btn{background:var(--accent);color:#fff;border:none;padding:4px 10px;border-radius:6px;font-size:13px;cursor:pointer;margin-left:6px}.dl-btn:hover{opacity:.85}.dl-btn:disabled{opacity:.5}
.edit-btn{background:#6c757d;color:#fff;border:none;padding:4px 10px;border-radius:6px;font-size:13px;cursor:pointer;margin-left:6px}
.mode-btn{background:#e9ecef;border:none;padding:6px 14px;border-radius:8px;cursor:pointer;font-size:13px}.mode-btn.active{background:var(--accent);color:#fff}
.modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.4);z-index:100}.modal-overlay.show{display:flex;justify-content:center;align-items:center}
.modal{background:#fff;border-radius:12px;padding:24px;max-width:700px;width:90%;max-height:85vh;overflow-y:auto}
.modal h2{margin:0 0 16px}.modal label{display:block;margin:8px 0 4px;font-weight:600;font-size:13px;color:var(--muted)}
.modal input,.modal textarea,.modal select{width:100%;margin-bottom:8px}.modal-actions{display:flex;gap:10px;margin-top:16px;justify-content:flex-end}
.btn-save{background:var(--accent);color:#fff;border:none;padding:8px 20px;border-radius:8px;cursor:pointer}.btn-cancel{background:#e9ecef;border:none;padding:8px 20px;border-radius:8px;cursor:pointer}
.kw-edit-input{font-size:12px;padding:2px 6px;border:1px solid var(--accent);border-radius:4px;width:100%}
.report-btn{background:#2e4a8b;color:#fff;border:none;padding:4px 10px;border-radius:6px;font-size:13px;cursor:pointer;margin-left:6px}.report-btn:hover{opacity:.85}.report-btn:disabled{opacity:.4}
.report-modal{background:#fff;border-radius:12px;padding:28px;max-width:900px;width:92%;max-height:88vh;overflow-y:auto;line-height:1.7}
.report-modal h1{font-size:20px;margin:0 0 4px}.report-modal h2{font-size:17px;margin:18px 0 8px;color:#176b5b}.report-modal h3{font-size:15px;margin:14px 0 6px}
.report-modal p,.report-modal li{font-size:14px;margin:6px 0}.report-modal code{background:#f0f0f0;padding:1px 4px;border-radius:3px;font-size:13px}.report-modal pre{background:#f6f8fa;padding:12px;border-radius:8px;overflow-x:auto}
.report-modal .modal-actions{margin-top:20px}
.topic-header{display:flex;align-items:center;gap:12px;background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:12px}.topic-header h2{margin:0;flex:1}.review-status{color:var(--muted);margin:8px 0}.topic-review{background:#fff;border:1px solid var(--line);border-radius:10px;padding:20px;margin:12px 0}.add-topic-btn{background:#8a5b18;color:#fff;border:0;padding:4px 10px;margin-left:6px}.remove-topic-btn{background:#9b3b3b;color:#fff;border:0;padding:4px 10px;margin-left:6px}
.readonly .admin-only{display:none!important}.markdown-editor{width:100%;min-height:360px;font-family:ui-monospace,SFMono-Regular,monospace}.auth-status{margin-left:auto}
@media(max-width:800px){.layout{grid-template-columns:1fr}.sidebar{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line)}header{position:static}}
</style></head><body>
<header><div class="bar"><h1>PaperLab</h1>
<input id="q" placeholder="Search: title, author, abstract, keywords... (Enter for semantic search)">
<select id="venue"><option value="">All venues</option></select>
<select id="year"><option value="">All years</option></select>
<select id="org"><option value="">All orgs</option></select>
<span id="stat" class="stat"></span>
<button id="llmSettings" class="admin-only" title="Report model settings">LLM Settings</button>
<button id="syncButton" class="admin-only">Sync</button>
<button id="authButton" class="auth-status">Admin login</button>
</div></header>
<div class="layout"><aside class="sidebar"><div class="home-link topic-item" onclick="showLibrary()">All Papers</div><div class="sidebar-head"><h2>Topics</h2><button class="admin-only" onclick="createTopic()" title="New topic">＋</button></div><div id="topicList"></div></aside>
<main><section id="topicHeader" class="topic-header" style="display:none"><h2 id="topicTitle"></h2><button class="edit-btn admin-only" onclick="renameTopic()">Rename</button><button id="reviewButton" class="btn-save admin-only" onclick="topicReviewAction()">Generate review</button></section><div id="reviewStatus" class="review-status"></div><section id="topicReview" class="topic-review" style="display:none"></section><div id="papers"></div>
<div class="pager"><button id="prev">Prev</button><span id="page"></span><button id="next">Next</button></div>
</main></div>
<div class="modal-overlay" id="editModal">
<div class="modal">
<h2>Edit Paper</h2>
<input type="hidden" id="edit-id">
<label>Title</label><textarea id="edit-title" rows="2"></textarea>
<label>Authors</label><input id="edit-authors" type="text">
<label>Organizations</label><input id="edit-organizations" type="text">
<label>Abstract</label><textarea id="edit-abstract" rows="6"></textarea>
<label>Keywords (comma-separated)</label><input id="edit-keywords" type="text">
<label>DOI</label><input id="edit-doi" type="text">
<label>arXiv ID</label><input id="edit-arxiv_id" type="text">
<label>Source URL</label><input id="edit-source_url" type="text">
<label>PDF URL</label><input id="edit-pdf_url" type="text">
<label>Awards</label><input id="edit-awards" type="text">
<div class="modal-actions">
<button class="btn-cancel" onclick="closeModal()">Cancel</button>
<button class="btn-save" onclick="savePaper()">Save</button>
</div>
</div>
</div>
<div class="modal-overlay" id="llmModal">
<div class="modal">
<h2>Report model</h2>
<label for="llm-provider">Provider</label>
<select id="llm-provider" aria-label="Provider"><option value="zhipu">Zhipu GLM</option><option value="cc_switch">CC Switch</option></select>
<label for="llm-model">Model</label><input id="llm-model" aria-label="Model" type="text" autocomplete="off">
<label for="llm-base-url">Base URL</label><input id="llm-base-url" aria-label="Base URL" type="url" autocomplete="off">
<label for="llm-api-key">API key</label><input id="llm-api-key" aria-label="API key" type="password" autocomplete="new-password" placeholder="Leave empty to keep the current key">
<div id="llm-key-status" class="stat"></div>
<div class="modal-actions">
<button class="btn-cancel" onclick="closeLLMSettings()">Cancel</button>
<button class="btn-save" onclick="saveLLMSettings()">Save</button>
</div>
</div>
</div>
<div class="modal-overlay" id="reportModal">
<div class="modal report-modal">
<div id="reportLoading" style="text-align:center;padding:40px;color:var(--muted)">Generating report, please wait...</div>
<div id="reportContent" style="display:none"></div>
<div class="modal-actions">
<button class="btn-cancel" onclick="closeReport()">Close</button>
</div>
</div>
</div>
<div class="modal-overlay" id="topicModal"><div class="modal"><h2>Add to topic</h2><input type="hidden" id="topic-paper-id"><div id="topicChoices"></div><div class="modal-actions"><button class="btn-cancel" onclick="closeTopicPicker()">Close</button></div></div></div>
<div class="modal-overlay" id="createTopicModal"><div class="modal"><h2>New topic</h2><label for="new-topic-name">Name</label><input id="new-topic-name" type="text" maxlength="120"><div class="modal-actions"><button class="btn-cancel" onclick="closeCreateTopic()">Cancel</button><button class="btn-save" onclick="submitCreateTopic()">Create</button></div></div></div>
<div class="modal-overlay" id="loginModal"><div class="modal"><h2>Administrator login</h2><label for="admin-username">Username</label><input id="admin-username" value="admin" autocomplete="username"><label for="admin-password">Password</label><input id="admin-password" type="password" autocomplete="current-password"><div id="loginError" class="review-status"></div><div class="modal-actions"><button class="btn-cancel" onclick="closeLogin()">Cancel</button><button class="btn-save" onclick="login()">Login</button></div></div></div>
<div class="modal-overlay" id="markdownModal"><div class="modal report-modal"><h2 id="markdownTitle">Edit Markdown</h2><textarea id="markdownContent" class="markdown-editor"></textarea><details><summary>Version history</summary><div id="historyList"></div></details><div class="modal-actions"><button class="btn-cancel" onclick="closeMarkdownEditor()">Cancel</button><button class="btn-save" onclick="previewMarkdown()">Preview</button><button class="btn-save" onclick="saveMarkdown()">Save</button></div><div id="markdownPreview" class="topic-review" style="display:none"></div></div></div>
<div class="modal-overlay" id="syncModal"><div class="modal report-modal"><h2>Synchronization</h2><div id="syncStatus" class="stat">Loading…</div><h3>Open conflicts</h3><div id="conflictList"></div><h3>Register local device</h3><input id="deviceName" placeholder="MacBook"><div class="modal-actions"><button class="btn-cancel" onclick="closeSync()">Close</button><button class="btn-save" onclick="registerDevice()">Create device token</button></div><pre id="deviceToken" style="display:none"></pre></div></div>
<script>
function openInEditor(path){fetch("/api/open_file",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:path})}).catch(function(){alert("Could not open file: "+path)});}
const S={page:1,pages:1,mode:"library",llm:null,topics:[],activeTopic:null,reviewTimer:null,visiblePapers:[],topicReviewAvailable:false,admin:false,csrf:null,markdown:null};
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const nativeFetch=window.fetch.bind(window);window.fetch=(url,opts={})=>{opts={...opts};const method=(opts.method||"GET").toUpperCase();if(method!=="GET"&&S.csrf){opts.headers={...(opts.headers||{}),"X-CSRF-Token":S.csrf};}return nativeFetch(url,opts);};
async function loadSession(){const d=await nativeFetch("/api/auth/session").then(r=>r.json());S.admin=!!d.authenticated;S.csrf=d.csrf_token||null;document.body.classList.toggle("readonly",!S.admin);$("authButton").textContent=S.admin?(d.trusted_local?"Local admin":"Logout"):"Admin login";}
function closeLogin(){$("loginModal").classList.remove("show");}
async function login(){const r=await nativeFetch("/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({username:$("admin-username").value,password:$("admin-password").value})});const d=await r.json();if(!r.ok){$("loginError").textContent=d.error||"Login failed";return;}S.csrf=d.csrf_token;closeLogin();await loadSession();await loadTopics();load(true);}
async function authAction(){if(!S.admin){$("admin-password").value="";$("loginError").textContent="";$("loginModal").classList.add("show");return;}if($("authButton").textContent==="Local admin")return;await fetch("/api/auth/logout",{method:"POST"});S.csrf=null;await loadSession();load(true);}
async function openSync(){$("syncModal").classList.add("show");const [status,conflicts]=await Promise.all([fetch("/api/sync/status").then(r=>r.json()),fetch("/api/sync/conflicts").then(r=>r.json())]);$("syncStatus").textContent="Sequence "+status.latest_sequence+" · "+status.devices.length+" devices · "+status.pending_uploads+" pending uploads · "+status.open_conflicts+" conflicts"+(status.last_success?" · last sync "+new Date(status.last_success*1000).toLocaleString():"")+(status.last_error?" · error: "+status.last_error:"");$("conflictList").innerHTML=(conflicts.conflicts||[]).map(c=>'<div class="paper"><b>'+esc(c.entity_type+" · "+c.entity_id+" · "+c.field)+'</b><div class="modal-actions"><button onclick="resolveConflict('+c.id+',\'local\')">Keep local</button><button onclick="resolveConflict('+c.id+',\'remote\')">Use remote</button></div></div>').join("")||'<div class="stat">No open conflicts</div>';}
function closeSync(){$("syncModal").classList.remove("show");}
async function registerDevice(){const r=await fetch("/api/sync/devices",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:$("deviceName").value||"Local device"})});const d=await r.json();if(!r.ok){alert(d.error||"Could not create device");return;}$("deviceToken").textContent="Device ID: "+d.device_id+"\nToken (shown once): "+d.token;$("deviceToken").style.display="block";openSync();}
async function resolveConflict(id,strategy){const r=await fetch("/api/sync/conflicts/"+id+"/resolve",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({strategy})});if(!r.ok){alert("Could not resolve conflict");return;}openSync();}
async function facets(){
  const f=await fetch("/api/facets").then(r=>r.json());
  for(const v of f.venues) $("venue").insertAdjacentHTML("beforeend","<option>"+esc(v)+"</option>");
  for(const y of f.years) $("year").insertAdjacentHTML("beforeend","<option>"+y+"</option>");
  for(const o of f.orgs||[]) $("org").insertAdjacentHTML("beforeend","<option>"+esc(o)+"</option>");
  $("stat").textContent=f.total.toLocaleString()+" papers";
}
async function loadLLMConfig(){
  if(!S.admin)return null;
  const r=await fetch("/api/llm/config");
  const d=await r.json();
  if(!r.ok)throw new Error(d.error||"Could not load LLM settings");
  S.llm=d;
  const active=d.providers[d.active_provider];
  $("llmSettings").textContent="LLM: "+active.label;
  return d;
}
function showLLMProvider(){
  const name=$("llm-provider").value;
  const p=S.llm.providers[name];
  $("llm-model").value=p.model||"";
  $("llm-base-url").value=p.base_url||"";
  $("llm-api-key").value="";
  $("llm-key-status").textContent=p.has_api_key?"API key configured":"API key required";
}
async function openLLMSettings(){
  try{
    await loadLLMConfig();
    $("llm-provider").value=S.llm.active_provider;
    showLLMProvider();
    $("llmModal").classList.add("show");
  }catch(e){alert(e.message);}
}
function closeLLMSettings(){$("llmModal").classList.remove("show");}
async function saveLLMSettings(){
  const name=$("llm-provider").value;
  const provider={model:$("llm-model").value,base_url:$("llm-base-url").value};
  if($("llm-api-key").value)provider.api_key=$("llm-api-key").value;
  const payload={active_provider:name,providers:{}};payload.providers[name]=provider;
  try{
    const r=await fetch("/api/llm/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    const d=await r.json();
    if(!r.ok)throw new Error(d.error||"Save failed");
    S.llm=d;closeLLMSettings();await loadLLMConfig();
  }catch(e){alert("Error: "+e.message);}
}
async function loadTopics(){
  const d=await fetch("/api/topics").then(r=>r.json());
  S.topics=d.topics||[];
  $("topicList").innerHTML=S.topics.length?S.topics.map(t=>'<div class="topic-item '+(S.activeTopic===t.id?'active':'')+'" onclick="openTopic('+t.id+')"><span class="topic-name">'+esc(t.name)+'</span><span class="topic-count">'+t.paper_count+'</span>'+(S.admin?'<button class="topic-delete" title="Delete" onclick="event.stopPropagation();deleteTopic('+t.id+')">×</button>':'')+'</div>').join(""):'<div class="stat">No topics yet</div>';
}
function createTopic(){$("new-topic-name").value="";$("createTopicModal").classList.add("show");$("new-topic-name").focus();}
function closeCreateTopic(){$("createTopicModal").classList.remove("show");}
async function submitCreateTopic(){
  const name=$("new-topic-name").value;
  const r=await fetch("/api/topics",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:name})});
  const d=await r.json();if(!r.ok){alert(d.error||"Could not create topic");return;}closeCreateTopic();await loadTopics();openTopic(d.id);
}
async function deleteTopic(id){
  const found=S.topics.find(t=>t.id===id);const name=found?found.name:"topic";
  if(!confirm('Delete topic "'+name+'"? Papers and their reports will be kept.'))return;
  const r=await fetch("/api/topics/"+id,{method:"DELETE"});const d=await r.json();
  if(!r.ok){alert(d.error||"Delete failed");return;}if(S.activeTopic===id)showLibrary();await loadTopics();
}
function showLibrary(){
  S.mode="library";S.activeTopic=null;clearInterval(S.reviewTimer);S.reviewTimer=null;
  S.topicReviewAvailable=false;
  $("topicHeader").style.display="none";$("topicReview").style.display="none";$("reviewStatus").textContent="";
  ["q","venue","year","org"].forEach(id=>$(id).disabled=false);loadTopics();load(true);
}
async function openTopic(id){
  S.mode="topic";S.activeTopic=id;S.topicReviewAvailable=false;clearInterval(S.reviewTimer);S.reviewTimer=null;
  ["q","venue","year","org"].forEach(x=>$(x).disabled=true);
  const r=await fetch("/api/topics/"+id+"/papers");const d=await r.json();
  if(S.mode!=="topic"||S.activeTopic!==id)return;
  if(!r.ok){alert(d.error||"Topic could not be loaded");showLibrary();return;}
  $("topicTitle").textContent=d.topic.name+" ("+d.total+")";$("topicHeader").style.display="flex";
  S.visiblePapers=d.papers;$("papers").innerHTML=d.papers.length?d.papers.map(card).join(""):'<div class="empty">This topic has no papers</div>';
  $("page").textContent=d.total+" papers";$("prev").disabled=true;$("next").disabled=true;
  await loadTopics();await pollTopicReview();window.scrollTo({top:0});
}
function openTopicPickerById(id){const p=S.visiblePapers.find(x=>x.id===id);if(p)openTopicPicker(p);}
function openTopicPicker(p){
  $("topic-paper-id").value=p.id;const ids=String(p.collection_topic_ids||"").split(",").filter(Boolean).map(Number);
  $("topicChoices").innerHTML=S.topics.length?S.topics.map(t=>'<label style="display:flex;gap:8px;align-items:center"><input style="width:auto" type="checkbox" '+(ids.includes(t.id)?'checked':'')+' onchange="togglePaperTopic('+t.id+','+JSON.stringify(p.id).replace(/"/g,'&quot;')+',this.checked)">'+esc(t.name)+'</label>').join(""):'<div class="stat">Create a topic from the sidebar first.</div>';
  $("topicModal").classList.add("show");
}
function closeTopicPicker(){$("topicModal").classList.remove("show");}
async function togglePaperTopic(topicId,paperId,add){
  const r=await fetch("/api/topics/"+topicId+"/papers/"+encodeURIComponent(paperId),{method:add?"POST":"DELETE"});const d=await r.json();
  if(!r.ok){alert(d.error||"Update failed");return;}await loadTopics();if(S.mode==="topic")await openTopic(S.activeTopic);
}
async function removeFromTopic(paperId){
  await togglePaperTopic(S.activeTopic,paperId,false);
}
async function startTopicReview(){
  if(!S.activeTopic)return;if(!S.llm)await loadLLMConfig();const active=S.llm.active_provider;
  const r=await fetch("/api/topics/"+S.activeTopic+"/review",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({provider:active,model:S.llm.providers[active].model})});
  const d=await r.json();if(!r.ok){let message=d.error||"Could not start review";if(d.missing_reports&&d.missing_reports.length)message+="\n\nMissing reports:\n"+d.missing_reports.map(x=>"• "+x.title).join("\n");alert(message);return;}await pollTopicReview();
  clearInterval(S.reviewTimer);S.reviewTimer=setInterval(pollTopicReview,1500);
}
function topicReviewAction(){
  if(S.topicReviewAvailable){$("topicReview").scrollIntoView({behavior:"smooth",block:"start"});return;}
  startTopicReview();
}
async function pollTopicReview(){
  if(!S.activeTopic)return;const id=S.activeTopic;const r=await fetch("/api/topics/"+id+"/review");const d=await r.json();if(!r.ok||id!==S.activeTopic)return;
  const running=d.status==="running";S.topicReviewAvailable=!!d.content;$("reviewButton").disabled=running;$("reviewButton").textContent=S.topicReviewAvailable?"Open review":"Generate review";
  $("reviewStatus").textContent=running?(d.stage+" · "+d.completed_papers+" / "+d.total_papers):(d.status==="error"?("Generation failed: "+d.error):(d.status==="stale"?"Review is out of date because membership changed.":""));
  if(d.content){$("topicReview").innerHTML=renderMarkdown(d.content)+(S.admin?'<div class="modal-actions"><button class="edit-btn" onclick="editReview('+id+','+(d.version||1)+')">Edit Markdown</button><button class="btn-save" onclick="startTopicReview()">Regenerate review</button></div>':'');$("topicReview").style.display="block";}else{$("topicReview").style.display="none";}
  if(!running&&S.reviewTimer){clearInterval(S.reviewTimer);S.reviewTimer=null;}await loadTopics();
}
function links(p){
  let r=[];
  if(p.source_url) r.push('<a class="link-btn" target="_blank" href="'+esc(p.source_url)+'">Link</a>');
  if(p.arxiv_id) r.push('<a class="link-btn" target="_blank" href="https://arxiv.org/abs/'+esc(p.arxiv_id)+'">arXiv</a>');
  return r.join(" ");
}
function dlButton(p){
  if(p.has_pdf) return '<button class="dl-btn" onclick="openPDF(\''+p.id+'\')">Open PDF</button>';
  return S.admin?'<button class="dl-btn" onclick="downloadPaper(\''+p.id+'\',this)">Download</button>':'';
}
function reportButton(p){
  if(p.has_report) return '<button class="report-btn" onclick="openReport(\''+p.id+'\')">Open report</button>';
  if(!S.admin)return '';
  if(!p.has_pdf) return '<button class="report-btn" disabled title="PDF required">Generate report</button>';
  return '<button class="report-btn" onclick="openReport(\''+p.id+'\')">Generate report</button>';
}
function openPDF(id){window.open("/pdf/"+encodeURIComponent(id),"_blank");}
async function downloadPaper(id,btn){
  btn.disabled=true;btn.textContent="Downloading...";
  try{
    const r=await fetch("/api/download",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({paper_id:id})});
    const d=await r.json();
    if(d.status==="ok"&&d.url){window.open(d.url,"_blank");btn.textContent="Open PDF";btn.disabled=false;}
    else{btn.textContent="Failed";btn.disabled=false;}
  }catch(e){btn.textContent="Error";btn.disabled=false;}
}
function editPaper(p){
  $("edit-id").value=p.id;
  $("edit-id").dataset.version=p.version||1;
  $("edit-title").value=p.title||"";
  $("edit-authors").value=p.authors||"";
  $("edit-organizations").value=p.organizations||"";
  $("edit-abstract").value=p.abstract||"";
  $("edit-keywords").value=p.keywords||"";
  $("edit-doi").value=p.doi||"";
  $("edit-arxiv_id").value=p.arxiv_id||"";
  $("edit-source_url").value=p.source_url||"";
  $("edit-pdf_url").value=p.pdf_url||"";
  $("edit-awards").value=p.awards||"";
  $("editModal").classList.add("show");
}
function closeModal(){$("editModal").classList.remove("show");}
async function savePaper(){
  const id=$("edit-id").value;
  const fields={
    title:$("edit-title").value,authors:$("edit-authors").value,
    organizations:$("edit-organizations").value,abstract:$("edit-abstract").value,
    keywords:$("edit-keywords").value,doi:$("edit-doi").value,
    arxiv_id:$("edit-arxiv_id").value,source_url:$("edit-source_url").value,
    pdf_url:$("edit-pdf_url").value,awards:$("edit-awards").value
  };
  try{
    const r=await fetch("/api/edit",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({paper_id:id,fields:fields,base_version:Number($("edit-id").dataset.version||1)})});
    const d=await r.json();
    if(d.updated){closeModal();load();}
    else{alert("Save failed");}
  }catch(e){alert("Error: "+e);}
}
function kwChips(p){
  return (p.keywords||"").split(",").filter(Boolean).map(k=>{
    return S.admin?'<span class="chip kw-chip" title="Click to edit keywords" onclick=\'editPaper('+JSON.stringify(p)+')\'>'+esc(k)+'</span>':'<span class="chip">'+esc(k)+'</span>';
  }).join(" ");
}
function renderMarkdown(md){
  let h=esc(md);
  h=h.replace(/!\[([^\]]*)\]\((?!https?:|\/)([^)]+)\)/g, function(match, alt, src) {
    return '<img alt="'+alt+'" src="/reports/'+src+'" style="max-width:100%;border:1px solid #dfe3e8;border-radius:8px;margin:8px 0" />';
  });
  h=h.replace(/^### (.+)$/gm,'<h3>$1</h3>');
  h=h.replace(/^## (.+)$/gm,'<h2>$1</h2>');
  h=h.replace(/^# (.+)$/gm,'<h1>$1</h1>');
  h=h.replace(/```([\s\S]*?)```/g,'<pre><code>$1</code></pre>');
  h=h.replace(/`([^`]+)`/g,'<code>$1</code>');
  h=h.replace(/!\[([^\]]*)\]\((?!https?:|\/)([^)]+)\)/g, function(match, alt, src) {
    return '<img alt="'+alt+'" src="/reports/'+src+'" style="max-width:100%;border:1px solid #dfe3e8;border-radius:8px;margin:8px 0" />';
  });
  h=h.replace(/^\- (.+)$/gm,'<li>$1</li>');
  h=h.replace(/^\d+\. (.+)$/gm,'<li>$1</li>');
  h=h.replace(/(<li>[\s\S]*?<\/li>)/g,'<ul>$1</ul>');
  h=h.replace(/<\/ul>\s*<ul>/g,'');
  h=h.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
  h=h.replace(/([^<>\n])\n\n/g,'$1</p><p>');
  h='<p>'+h+'</p>';
  return h.replace(/<p>\s*<(h[123]|ul|pre)/g,'<$1').replace(/<\/(h[123]|ul|pre)>\s*<\/p>/g,'</$1>');
}
async function openReport(id){
  $("reportLoading").style.display="block";
  $("reportContent").style.display="none";
  $("reportModal").classList.add("show");
  try{
    const r=await fetch("/api/report?id="+encodeURIComponent(id));
    const d=await r.json();
    if(r.ok&&d.status==="ok"&&d.content){
      const html=renderMarkdown(d.content);
      $("reportContent").innerHTML=html+(S.admin?'<div class="modal-actions" style="margin-top:16px;border-top:1px solid #dfe3e8;padding-top:12px;display:flex;justify-content:flex-end;gap:8px"><button class="edit-btn" onclick="editReport('+JSON.stringify(id)+','+(d.version||1)+')">Edit Markdown</button><button class="btn-save" onclick="generateReport('+JSON.stringify(id)+')">&#x21bb; Regenerate</button></div>':'');
      $("reportLoading").style.display="none";
      $("reportContent").style.display="block";
    }else if(r.ok&&d.status==="none"){
      if(S.admin)await generateReport(id);else $("reportLoading").textContent="No report is available.";
    }else{
      $("reportLoading").textContent=d.error||"Report could not be loaded.";
    }
  }catch(e){
    $("reportLoading").textContent="Error: "+e;
  }
}

async function load(reset){
  if(S.mode==="topic")return openTopic(S.activeTopic);
  if(reset)S.page=1;
  const p=new URLSearchParams({page:S.page,per_page:"50",q:$("q").value,venue:$("venue").value,year:$("year").value,org:$("org").value});
  const d=await fetch("/api/papers?"+p).then(r=>r.json());
  if(S.mode!=="library")return;
  S.pages=d.pages||1;
  S.visiblePapers=d.papers;$("papers").innerHTML=d.papers.length?d.papers.map(card).join(""):'<div class="empty">No matching papers</div>';
  $("page").textContent=d.page+" / "+S.pages+" ("+d.total.toLocaleString()+" papers)";
  $("prev").disabled=S.page<=1;$("next").disabled=S.page>=S.pages;
  window.scrollTo({top:0});
}
async function semanticSearch(){
  const q=$("q").value.trim();
  if(!q){load(true);return;}
  const p=new URLSearchParams({q:q,venue:$("venue").value,year:$("year").value,org:$("org").value});
  const d=await fetch("/api/search?"+p).then(r=>r.json());
  if(S.mode!=="library")return;
  S.pages=1;
  const matches=d.matches||[];
  S.visiblePapers=matches;$("papers").innerHTML=matches.length?matches.map(card).join(""):'<div class="empty">No matching papers</div>';
  $("page").textContent=matches.length+" results (semantic)";
  $("prev").disabled=true;$("next").disabled=true;
}
let timer;
function onInput(){
  clearTimeout(timer);
  timer=setTimeout(()=>load(true),300);
}
async function generateReport(id){
  $("reportLoading").style.display="block";
  $("reportLoading").textContent="Generating report, please wait...";
  $("reportContent").style.display="none";
  try{
    if(!S.llm)await loadLLMConfig();
    const active=S.llm.active_provider;
    const r=await fetch("/api/report",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({paper_id:id,provider:active,model:S.llm.providers[active].model})});
    const d=await r.json();
    if(r.ok&&d.status==="ok"){
      const r2=await fetch("/api/report?id="+encodeURIComponent(id));
      const d2=await r2.json();
      if(d2.status==="ok"&&d2.content){
        openReport(id);
      }else{
        $("reportLoading").textContent="Report generated but could not be loaded.";
      }
    }else{
      $("reportLoading").textContent=d.error||"Generation failed";
    }
  }catch(e){
    $("reportLoading").textContent="Error: "+e;
  }
}
function closeReport(){$("reportModal").classList.remove("show");}
function card(p){
  const m=(p.venue_memberships||(p.venue||"")+" "+(p.year||"")).split("|").filter(Boolean).map(x=>'<span class="chip">'+esc(x)+'</span>').join("");
  let html='<article class="paper"><div class="title">'+esc(p.title)+'</div>';
  html+='<div class="meta">'+m+" "+kwChips(p)+'</div>';
  html+='<div class="authors">'+esc(p.authors||"")+'</div>';
  if(p.organizations) html+='<div class="orgs">'+esc(p.organizations)+'</div>';
  if(p.abstract) html+='<details><summary>Abstract</summary><div class="abstract">'+esc(p.abstract)+'</div></details>';
  const topicAction=!S.admin?'':(S.mode==="topic"?'<button class="remove-topic-btn" onclick=\'removeFromTopic('+JSON.stringify(p.id)+')\'>Remove from topic</button>':'<button class="add-topic-btn" onclick=\'openTopicPickerById('+JSON.stringify(p.id)+')\'>Add to topic</button>');
  html+='<div class="links">'+links(p)+" "+dlButton(p)+" "+reportButton(p)+topicAction+(S.admin?'<button class="edit-btn" onclick="editPaper('+esc(JSON.stringify(p))+')">Edit</button>':'')+'</div></article>';
  return html;
}

$("q").addEventListener("input",onInput);
$("q").addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();semanticSearch();}});
["venue","year","org"].forEach(id=>$(id).addEventListener("change",()=>load(true)));
$("prev").onclick=()=>{S.page--;load();};
$("next").onclick=()=>{S.page++;load();};
$("reportModal").addEventListener("click",e=>{if(e.target===$("reportModal"))closeReport();});
$("llmModal").addEventListener("click",e=>{if(e.target===$("llmModal"))closeLLMSettings();});
$("llmSettings").onclick=openLLMSettings;$("authButton").onclick=authAction;$("syncButton").onclick=openSync;
$("llm-provider").addEventListener("change",showLLMProvider);
$("new-topic-name").addEventListener("keydown",e=>{if(e.key==="Enter")submitCreateTopic();});
async function editReport(id,version){const d=await fetch("/api/report?id="+encodeURIComponent(id)).then(r=>r.json());openMarkdownEditor("report",id,version,d.content||"");}
async function editReview(id,version){const d=await fetch("/api/topics/"+id+"/review").then(r=>r.json());openMarkdownEditor("review",id,version,d.content||"");}
async function openMarkdownEditor(type,id,version,content){S.markdown={type,id,version};$("markdownTitle").textContent="Edit "+type;$("markdownContent").value=content;$("markdownPreview").style.display="none";$("markdownModal").classList.add("show");const d=await fetch("/api/history/"+type+"/"+encodeURIComponent(id)).then(r=>r.json());$("historyList").innerHTML=(d.versions||[]).map((v,i)=>'<button onclick="restoreHistory('+i+')">Version '+v.version+' · '+new Date(v.created_at*1000).toLocaleString()+'</button>').join(" ");S.markdown.history=d.versions||[];}
function restoreHistory(index){if(S.markdown&&S.markdown.history[index])$("markdownContent").value=S.markdown.history[index].content;}
function closeMarkdownEditor(){$("markdownModal").classList.remove("show");S.markdown=null;}
function previewMarkdown(){$("markdownPreview").innerHTML=renderMarkdown($("markdownContent").value);$("markdownPreview").style.display="block";}
async function saveMarkdown(){if(!S.markdown)return;const m=S.markdown;const url=m.type==="report"?"/api/reports/"+encodeURIComponent(m.id):"/api/topics/"+m.id+"/review";const r=await fetch(url,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({content:$("markdownContent").value,base_version:m.version})});const d=await r.json();if(!r.ok){alert(d.error||"Save failed");return;}closeMarkdownEditor();if(m.type==="report")openReport(m.id);else pollTopicReview();}
async function renameTopic(){const t=S.topics.find(x=>x.id===S.activeTopic);if(!t)return;const name=prompt("Topic name",t.name);if(!name||name===t.name)return;const r=await fetch("/api/topics/"+t.id,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,base_version:t.version||1})});const d=await r.json();if(!r.ok){alert(d.error||"Rename failed");return;}await loadTopics();await openTopic(t.id);}
loadSession().then(()=>Promise.all([facets(),loadLLMConfig(),loadTopics()])).then(()=>load());
</script>
</body>
</html>
"""
