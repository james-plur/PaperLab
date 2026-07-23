"""Fast local Web UI backed directly by paginated SQLite queries."""
from __future__ import annotations

import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def _connect(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def query_papers(db_path, params):
    def one(name, default=""):
        return str((params.get(name) or [default])[0]).strip()

    page = max(1, int(one("page", "1") or 1))
    per_page = min(100, max(10, int(one("per_page", "50") or 50)))
    clauses = ["1=1"]
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
    where = " AND ".join(clauses)
    conn = _connect(db_path)
    try:
        total = conn.execute("SELECT count(*) FROM papers p WHERE " + where, values).fetchone()[0]
        rows = conn.execute(
            "SELECT p.id,p.title,p.authors,p.organizations,p.awards,p.venue,p.year,"
            "p.abstract,p.doi,p.arxiv_id,p.source_url,p.pdf_url,p.pdf_path,"
            "p.metadata_status,p.citation_count,p.keywords,"
            "(SELECT group_concat(pv.venue || ' ' || pv.year,'|') FROM paper_venues pv WHERE pv.paper_id=p.id) venue_memberships,"
            "(SELECT group_concat(pt.topic_id || ':' || printf('%.2f',pt.score),'|') FROM paper_topics pt WHERE pt.paper_id=p.id AND pt.relevant=1) topics "
            "FROM papers p WHERE " + where + " ORDER BY p.year DESC,p.venue,p.title LIMIT ? OFFSET ?",
            [*values, per_page, (page - 1) * per_page]).fetchall()
    finally:
        conn.close()
    return {"page": page, "per_page": per_page, "total": total,
            "pages": (total + per_page - 1) // per_page,
            "papers": [dict(r) for r in rows]}


def query_facets(db_path):
    conn = _connect(db_path)
    try:
        total = conn.execute("SELECT count(*) FROM papers").fetchone()[0]
        venues = [r[0] for r in conn.execute("SELECT DISTINCT venue FROM paper_venues WHERE venue!='' ORDER BY venue")]
        years = [r[0] for r in conn.execute("SELECT DISTINCT year FROM paper_venues ORDER BY year DESC")]
    finally:
        conn.close()
    return {"total": total, "venues": venues, "years": years}


def semantic_search(db_path, description, limit=50):
    """Fast keyword search with Chinese support and keyword variants."""
    import re as _re

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

    # English 2-word and 3-word phrases
    for i in range(len(en_words) - 1):
        search_kws.add(en_words[i] + " " + en_words[i + 1])
    for i in range(len(en_words) - 2):
        search_kws.add(en_words[i] + " " + en_words[i + 1] + " " + en_words[i + 2])

    # Add variants: "kv cache" -> also "kvcache"
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

    # Collect candidates: keywords column first (most specific), then title, then abstract
    conn = _connect(db_path)
    candidates = []
    seen_ids = set()
    try:
        # 1. Keywords column (specific, high-value matches)
        for kw in kw_list:
            token = "%" + kw + "%"
            rows = conn.execute(
                "SELECT id,title,authors,venue,year,abstract,doi,arxiv_id,source_url,"
                "pdf_path,pdf_url,keywords,"
                "(SELECT group_concat(pv.venue||' '||pv.year,'|') FROM paper_venues pv WHERE pv.paper_id=papers.id) venue_memberships "
                "FROM papers WHERE keywords IS NOT NULL AND lower(keywords) LIKE ? "
                "ORDER BY year DESC,title LIMIT 30",
                (token,)).fetchall()
            for r in rows:
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    candidates.append(dict(r))
        # 2. Title column
        for kw in kw_list:
            token = "%" + kw + "%"
            rows = conn.execute(
                "SELECT id,title,authors,venue,year,abstract,doi,arxiv_id,source_url,"
                "pdf_path,pdf_url,keywords,"
                "(SELECT group_concat(pv.venue||' '||pv.year,'|') FROM paper_venues pv WHERE pv.paper_id=papers.id) venue_memberships "
                "FROM papers WHERE lower(title) LIKE ? "
                "ORDER BY year DESC,title LIMIT 30",
                (token,)).fetchall()
            for r in rows:
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    candidates.append(dict(r))
        # 3. Abstract (broadest, cap at 5 keywords)
        for kw in kw_list[:5]:
            token = "%" + kw + "%"
            rows = conn.execute(
                "SELECT id,title,authors,venue,year,abstract,doi,arxiv_id,source_url,"
                "pdf_path,pdf_url,keywords,"
                "(SELECT group_concat(pv.venue||' '||pv.year,'|') FROM paper_venues pv WHERE pv.paper_id=papers.id) venue_memberships "
                "FROM papers WHERE lower(abstract) LIKE ? "
                "ORDER BY year DESC,title LIMIT 30",
                (token,)).fetchall()
            for r in rows:
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    candidates.append(dict(r))
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
    return candidates[:max(limit, 100)]


def update_paper(db_path, paper_id, fields):
    """Update editable fields of a paper, persist to DB."""
    allowed = ("title", "authors", "organizations", "abstract", "keywords",
               "doi", "arxiv_id", "source_url", "pdf_url", "awards")
    conn = _connect(db_path)
    try:
        sets = []
        vals = []
        for k in allowed:
            if k in fields:
                sets.append(k + "=?")
                vals.append(fields[k] if fields[k] else None)
        if not sets:
            return {"updated": 0}
        import time
        sets.append("last_updated=?")
        vals.append(time.time())
        vals.append(paper_id)
        conn.execute("UPDATE papers SET " + ",".join(sets) + " WHERE id=?", vals)
        conn.commit()
        row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        return {"updated": 1, "paper": dict(row) if row else None}
    finally:
        conn.close()


class PaperHandler(BaseHTTPRequestHandler):
    db_path = Path(".")

    def _send(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path.startswith("/pdf/"):
                import urllib.parse as ul
                pid = ul.unquote(parsed.path[5:])
                self._serve_pdf(pid)
            elif parsed.path == "/api/papers":
                self._send(json.dumps(query_papers(self.db_path, parse_qs(parsed.query)), ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            elif parsed.path == "/api/facets":
                self._send(json.dumps(query_facets(self.db_path), ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            elif parsed.path == "/api/search":
                import traceback as _tb
                try:
                    q = (parse_qs(parsed.query).get("q") or [""])[0]
                    results = semantic_search(self.db_path, q)
                    self._send(json.dumps({"matches": results, "total": len(results)}, ensure_ascii=False).encode(),
                               "application/json; charset=utf-8")
                except Exception as exc:
                    err = str(exc) + "\n" + _tb.format_exc()
                    print("[web] search error: " + err, flush=True)
                    self._send(json.dumps({"matches": [], "total": 0, "error": err}, ensure_ascii=False).encode(),
                               "application/json", 500)
            elif parsed.path in ("/", "/index.html"):
                self._send(HTML.encode(), "text/html; charset=utf-8")
            elif parsed.path == "/api/report":
                import urllib.parse as ul
                pid = ul.unquote(parse_qs(parsed.query).get("id", [""])[0])
                self._serve_report(pid)
            else:
                self._send(b"not found", "text/plain; charset=utf-8", 404)
        except Exception as exc:
            self._send(json.dumps({"error": str(exc)}).encode(), "application/json", 500)


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

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        data = json.loads(body) if body else {}
        try:
            if parsed.path == "/api/download":
                self._do_download(data.get("paper_id", ""))
            elif parsed.path == "/api/edit":
                result = update_paper(self.db_path, data.get("paper_id", ""), data.get("fields", {}))
                self._send(json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            elif parsed.path == "/api/report":
                self._do_report(data.get("paper_id", ""))
            else:
                self._send(b"not found", "text/plain; charset=utf-8", 404)
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
            import sys
            sys.path.insert(0, "/Users/luchenda/tools/paperlab")
            from .config import load_config
            from .store import Store
            from . import pipeline
            cfg = load_config("/Users/luchenda/tools/paperlab/topics.yaml")
            with Store(str(self.db_path)) as store:
                n = pipeline.download_single(store, cfg, pid, log=lambda m: None)
            if n:
                self._send(json.dumps({"status": "ok", "url": "/pdf/" + pid}).encode(), "application/json")
            else:
                self._send(json.dumps({"status": "failed"}).encode(), "application/json")
        except Exception as exc:
           self._send(json.dumps({"error": str(exc)}).encode(), "application/json", 500)

    def _serve_report(self, paper_id):
        """GET /api/report?id=...  -- return report markdown + status."""
        conn = _connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT pdf_path FROM papers WHERE id=?", (paper_id,)).fetchone()
            rpt = conn.execute(
                "SELECT report_path,status,generated_at FROM paper_reports WHERE paper_id=?",
                (paper_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            self._send(json.dumps({"error": "paper not found"}).encode(), "application/json", 404)
            return
        has_pdf = bool(row["pdf_path"]) and os.path.isfile(row["pdf_path"])
        if not has_pdf:
            self._send(json.dumps({"error": "PDF not downloaded; report requires the PDF"}).encode(),
                       "application/json", 400)
            return
        if rpt and rpt["report_path"] and os.path.isfile(rpt["report_path"]):
            content = open(rpt["report_path"], "r", encoding="utf-8").read()
            self._send(json.dumps({"status": "ok", "content": content,
                                   "path": rpt["report_path"]}).encode(),
                       "application/json; charset=utf-8")
            return
        self._send(json.dumps({"status": "none"}).encode(), "application/json; charset=utf-8")

    def _do_report(self, paper_id):
        """POST /api/report  -- generate (or regenerate) the deep-read report."""
        conn = _connect(self.db_path)
        try:
            row = conn.execute("SELECT pdf_path FROM papers WHERE id=?", (paper_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            self._send(json.dumps({"error": "paper not found"}).encode(), "application/json", 404)
            return
        if not row["pdf_path"] or not os.path.isfile(row["pdf_path"]):
            self._send(json.dumps({"error": "PDF not downloaded; report requires the PDF"}).encode(),
                       "application/json", 400)
            return
        try:
            import sys
            sys.path.insert(0, "/Users/luchenda/tools/paperlab")
            from .config import load_config
            from .store import Store
            from . import report as report_mod
            cfg = load_config("/Users/luchenda/tools/paperlab/topics.yaml")
            with Store(str(self.db_path)) as store:
                path = report_mod.generate_report(store, cfg, paper_id, log=lambda m: None)
            self._send(json.dumps({"status": "ok", "path": path}).encode(),
                       "application/json; charset=utf-8")
        except Exception as exc:
            self._send(json.dumps({"error": str(exc)}).encode(), "application/json", 500)

    def log_message(self, fmt, *args):
        print("[web] " + fmt % args, flush=True)


def serve(db_path, host="127.0.0.1", port=8765):
    import socketserver as _ss
    _ss.TCPServer.allow_reuse_address = True
    handler = type("ConfiguredPaperHandler", (PaperHandler,), {"db_path": Path(db_path)})
    server = ThreadingHTTPServer((host, port), handler)
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
main{max-width:1500px;margin:16px auto;padding:0 20px}.paper{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0}
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
.modal input,.modal textarea{width:100%;margin-bottom:8px}.modal-actions{display:flex;gap:10px;margin-top:16px;justify-content:flex-end}
.btn-save{background:var(--accent);color:#fff;border:none;padding:8px 20px;border-radius:8px;cursor:pointer}.btn-cancel{background:#e9ecef;border:none;padding:8px 20px;border-radius:8px;cursor:pointer}
.kw-edit-input{font-size:12px;padding:2px 6px;border:1px solid var(--accent);border-radius:4px;width:100%}
.report-btn{background:#2e4a8b;color:#fff;border:none;padding:4px 10px;border-radius:6px;font-size:13px;cursor:pointer;margin-left:6px}.report-btn:hover{opacity:.85}.report-btn:disabled{opacity:.4}
.report-modal{background:#fff;border-radius:12px;padding:28px;max-width:900px;width:92%;max-height:88vh;overflow-y:auto;line-height:1.7}
.report-modal h1{font-size:20px;margin:0 0 4px}.report-modal h2{font-size:17px;margin:18px 0 8px;color:#176b5b}.report-modal h3{font-size:15px;margin:14px 0 6px}
.report-modal p,.report-modal li{font-size:14px;margin:6px 0}.report-modal code{background:#f0f0f0;padding:1px 4px;border-radius:3px;font-size:13px}.report-modal pre{background:#f6f8fa;padding:12px;border-radius:8px;overflow-x:auto}
.report-modal .modal-actions{margin-top:20px}
</style></head><body>
<header><div class="bar"><h1>PaperLab</h1>
<input id="q" placeholder="Search: title, author, abstract, keywords... (Enter for semantic search)">
<select id="venue"><option value="">All venues</option></select>
<select id="year"><option value="">All years</option></select>
<span id="stat" class="stat"></span>
</div></header>
<main><div id="papers"></div>
<div class="pager"><button id="prev">Prev</button><span id="page"></span><button id="next">Next</button></div>
</main>
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
<div class="modal-overlay" id="reportModal">
<div class="modal report-modal">
<div id="reportLoading" style="text-align:center;padding:40px;color:var(--muted)">Generating report, please wait...</div>
<div id="reportContent" style="display:none"></div>
<div class="modal-actions">
<button class="btn-cancel" onclick="closeReport()">Close</button>
</div>
</div>
</div>
<script>
const S={page:1,pages:1,mode:"keyword"};
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function facets(){
  const f=await fetch("/api/facets").then(r=>r.json());
  for(const v of f.venues) $("venue").insertAdjacentHTML("beforeend","<option>"+esc(v)+"</option>");
  for(const y of f.years) $("year").insertAdjacentHTML("beforeend","<option>"+y+"</option>");
  $("stat").textContent=f.total.toLocaleString()+" papers";
}
function links(p){
  let r=[];
  if(p.source_url) r.push('<a class="link-btn" target="_blank" href="'+esc(p.source_url)+'">Link</a>');
  if(p.arxiv_id) r.push('<a class="link-btn" target="_blank" href="https://arxiv.org/abs/'+esc(p.arxiv_id)+'">arXiv</a>');
  return r.join(" ");
}
function dlButton(p){
  if(p.pdf_path) return '<button class="dl-btn" onclick="openPDF(\''+p.id+'\')">Open PDF</button>';
  return '<button class="dl-btn" onclick="downloadPaper(\''+p.id+'\',this)">Download</button>';
}
function reportButton(p){
  if(!p.pdf_path) return '<button class="report-btn" disabled title="PDF required">Report</button>';
  return '<button class="report-btn" onclick="openReport(\''+p.id+'\')">Report</button>';
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
    const r=await fetch("/api/edit",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({paper_id:id,fields:fields})});
    const d=await r.json();
    if(d.updated){closeModal();load();}
    else{alert("Save failed");}
  }catch(e){alert("Error: "+e);}
}
function kwChips(p){
  return (p.keywords||"").split(",").filter(Boolean).map(k=>{
    return '<span class="chip kw-chip" title="Click to edit keywords" onclick=\'editPaper('+JSON.stringify(p)+')\'>'+esc(k)+'</span>';
  }).join(" ");
}
function renderMarkdown(md){
  let h=esc(md);
  h=h.replace(/^### (.+)$/gm,'<h3>$1</h3>');
  h=h.replace(/^## (.+)$/gm,'<h2>$1</h2>');
  h=h.replace(/^# (.+)$/gm,'<h1>$1</h1>');
  h=h.replace(/```([\s\S]*?)```/g,'<pre><code>$1</code></pre>');
  h=h.replace(/`([^`]+)`/g,'<code>$1</code>');
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
    if(d.status==="ok"&&d.content){
      $("reportContent").innerHTML=renderMarkdown(d.content);
      $("reportLoading").style.display="none";
      $("reportContent").style.display="block";
    }else if(d.status==="none"){
      await generateReport(id);
    }else{
      $("reportLoading").textContent=d.error||"Report unavailable (PDF required)";
    }
  }catch(e){
    $("reportLoading").textContent="Error: "+e;
  }
}
async function generateReport(id){
  $("reportLoading").style.display="block";
  $("reportLoading").textContent="Generating report, please wait...";
  $("reportContent").style.display="none";
  try{
    const r=await fetch("/api/report",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({paper_id:id})});
    const d=await r.json();
    if(d.status==="ok"){
      const r2=await fetch("/api/report?id="+encodeURIComponent(id));
      const d2=await r2.json();
      if(d2.status==="ok"&&d2.content){
        $("reportContent").innerHTML=renderMarkdown(d2.content);
        $("reportLoading").style.display="none";
        $("reportContent").style.display="block";
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
 html+='<div class="links">'+links(p)+" "+dlButton(p)+" "+reportButton(p)+'<button class="edit-btn" onclick=\'editPaper('+JSON.stringify(p)+')\'>Edit</button></div></article>';
 return html;
}
async function load(reset){
  if(reset)S.page=1;
  const p=new URLSearchParams({page:S.page,per_page:"50",q:$("q").value,venue:$("venue").value,year:$("year").value});
  const d=await fetch("/api/papers?"+p).then(r=>r.json());
  S.pages=d.pages||1;
  $("papers").innerHTML=d.papers.length?d.papers.map(card).join(""):'<div class="empty">No matching papers</div>';
  $("page").textContent=d.page+" / "+S.pages+" ("+d.total.toLocaleString()+" papers)";
  $("prev").disabled=S.page<=1;$("next").disabled=S.page>=S.pages;
  window.scrollTo({top:0});
}
async function semanticSearch(){
  const q=$("q").value.trim();
  if(!q){load(true);return;}
  const d=await fetch("/api/search?q="+encodeURIComponent(q)).then(r=>r.json());
  S.pages=1;
  const matches=d.matches||[];
  $("papers").innerHTML=matches.length?matches.map(card).join(""):'<div class="empty">No matching papers</div>';
  $("page").textContent=matches.length+" results (semantic)";
  $("prev").disabled=true;$("next").disabled=true;
}
let timer;
function onInput(){
  clearTimeout(timer);
  timer=setTimeout(()=>load(true),300);
}
$("q").addEventListener("input",onInput);
$("q").addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();semanticSearch();}});
["venue","year"].forEach(id=>$(id).addEventListener("change",()=>load(true)));
$("prev").onclick=()=>{S.page--;load();};
$("next").onclick=()=>{S.page++;load();};
$("editModal").addEventListener("click",e=>{if(e.target===$("editModal"))closeModal();});
$("reportModal").addEventListener("click",e=>{if(e.target===$("reportModal"))closeReport();});
facets().then(()=>load());
</script></body></html>"""
