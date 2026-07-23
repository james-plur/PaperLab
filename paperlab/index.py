"""Static HTML index site generator.

Single-page tool UI: dense table, topic/venue/year filters + full-text search,
figure column (cropped PNG thumbnail or LLM Mermaid rendered via CDN), and a
JSON/JSONL data layer emitted alongside index.html.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .store import Store


def _paper_view(row, *, site_dir: Path) -> dict:
    d = dict(row)
    # relative path from site/index.html to the cropped figure png
    fp = d.get("figure_path")
    if fp:
        try:
            d["figure_rel"] = os.path.relpath(fp, site_dir)
        except ValueError:
            d["figure_rel"] = None
    else:
        d["figure_rel"] = None
    d["topic"] = (d.get("topics") or "").split(",")[0] or ""
    return d


def build(store: Store, topics: list[str] | None, *, site_dir: Path) -> Path:
    site_dir = Path(site_dir)
    site_dir.mkdir(parents=True, exist_ok=True)
    rows = store.all()
    if not topics:
        views = [_paper_view(r, site_dir=site_dir) for r in rows]
    else:
        tset = set(topics)
        views = []
        for r in rows:
            ts = {x for x in (r["topics"] or "").split(",") if x}
            if ts & tset:
                views.append(_paper_view(r, site_dir=site_dir))

    (site_dir / "data.json").write_text(
        json.dumps(views, ensure_ascii=False, default=str), encoding="utf-8")
    with open(site_dir / "papers.jsonl", "w", encoding="utf-8") as f:
        for p in views:
            f.write(json.dumps(p, ensure_ascii=False, default=str) + "\n")

    html = _render(views)
    out = site_dir / "index.html"
    out.write_text(html, encoding="utf-8")
    return out


def _render(views: list[dict]) -> str:
    topics = sorted({v["topic"] for v in views if v["topic"]})
    venues = sorted({(v["venue"] or "") for v in views if v["venue"]})
    years = sorted({v["year"] for v in views if v["year"]}, reverse=True)
    data = json.dumps(views, ensure_ascii=False)
    return _HTML.format(
        data=data,
        topic_opts=_opts(topics),
        venue_opts=_opts(venues),
        year_opts=_opts([str(y) for y in years]),
        total=len(views),
    )


def _opts(values: list[str]) -> str:
    return "".join(f"<option value='{v}'>{v}</option>" for v in values)


_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PaperLab index</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
:root {{
  --bg:#f6f7f9; --panel:#ffffff; --ink:#1f2430; --muted:#6b7280;
  --line:#e3e6ea; --accent:#0f766e; --accent2:#b45309; --chip:#eef2f4;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
header {{ position:sticky; top:0; z-index:5; background:var(--panel);
  border-bottom:1px solid var(--line); padding:10px 14px; }}
.top {{ display:flex; align-items:center; gap:14px; flex-wrap:wrap; }}
h1 {{ font-size:15px; margin:0; font-weight:650; letter-spacing:.2px; }}
.stat {{ color:var(--muted); font-variant-numeric:tabular-nums; }}
.filters {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:8px; }}
select, input {{ font:inherit; border:1px solid var(--line); border-radius:6px;
  padding:4px 7px; background:#fff; color:var(--ink); }}
input.search {{ flex:1; min-width:180px; }}
main {{ padding:0 14px 40px; }}
table {{ width:100%; border-collapse:collapse; background:var(--panel);
  border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
th, td {{ text-align:left; vertical-align:top; padding:7px 9px; border-bottom:1px solid var(--line); }}
th {{ background:#fbfbfc; font-weight:600; font-size:12px; color:var(--muted);
  text-transform:uppercase; letter-spacing:.4px; position:sticky; top:78px; z-index:2; }}
tbody tr:hover {{ background:#fafbfc; }}
.t-title {{ font-weight:600; max-width:340px; }}
.t-authors, .t-orgs {{ color:var(--muted); max-width:200px; }}
.t-problem, .t-core {{ max-width:300px; }}
.chip {{ display:inline-block; background:var(--chip); border-radius:10px;
  padding:1px 8px; font-size:11px; color:var(--ink); white-space:nowrap; }}
.v-arxiv {{ color:var(--accent2); }}
a {{ color:var(--accent); text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
img.thumb {{ width:120px; height:auto; max-height:90px; object-fit:contain;
  border:1px solid var(--line); border-radius:4px; cursor:zoom-in; background:#fff; }}
.figbtn {{ border:1px solid var(--accent); color:var(--accent); background:#fff;
  border-radius:6px; padding:3px 8px; cursor:pointer; font:inherit; }}
.figbtn:hover {{ background:var(--accent); color:#fff; }}
.none {{ color:var(--muted); font-style:italic; }}
#modal {{ display:none; position:fixed; inset:0; background:rgba(20,24,30,.55);
  z-index:20; align-items:center; justify-content:center; }}
#modal.on {{ display:flex; }}
.card {{ background:#fff; border-radius:10px; padding:14px; max-width:760px;
  width:92vw; max-height:88vh; overflow:auto; box-shadow:0 10px 40px rgba(0,0,0,.25); }}
.card img {{ width:100%; height:auto; }}
.card .mermaid {{ display:flex; justify-content:center; }}
.x {{ float:right; cursor:pointer; color:var(--muted); border:none; background:none; font:16px/1 inherit; }}
</style>
</head>
<body>
<header>
  <div class="top">
    <h1>PaperLab</h1>
    <span class="stat" id="stat">{total} papers</span>
    <div class="filters" style="margin:0">
      <select id="f-topic"><option value="">all topics</option>{topic_opts}</select>
      <select id="f-venue"><option value="">all venues</option>{venue_opts}</select>
      <select id="f-year"><option value="">all years</option>{year_opts}</select>
      <input class="search" id="f-q" placeholder="search title / authors / org / problem / core idea">
    </div>
  </div>
</header>
<main>
<table id="tbl">
  <thead><tr>
    <th>Topic</th><th>Venue</th><th>Year</th><th>Title</th>
    <th>Authors</th><th>Orgs</th><th>Target problem</th><th>Core idea</th>
    <th>GitHub</th><th>Framework</th>
  </tr></thead>
  <tbody id="rows"></tbody>
</table>
</main>
<div id="modal"><div class="card">
  <button class="x" onclick="closeModal()">&times;</button>
  <div id="modal-body"></div>
</div></div>
<script>
const PAPERS = {data};
mermaid.initialize({{startOnLoad:false}});

function esc(s) {{
  if (s==null) return '';
  return String(s).replace(/[&<>]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;'}})[c]);
}}
function chip(v, cls) {{ return v ? `<span class="chip ${{cls||''}}">${{esc(v)}}</span>` : ''; }}

function figCell(p) {{
  if (p.figure_kind === 'pdf' && p.figure_rel) {{
    return `<img class="thumb" src="${{p.figure_rel}}" alt="" onclick="openImg(p.figure_rel)">`;
  }} else if (p.figure_kind === 'mermaid' && p.figure_mermaid) {{
    return `<button class="figbtn" onclick="openMermaid(${{JSON.stringify(p.figure_mermaid)}})">diagram</button>`;
  }}
  return `<span class="none">-</span>`;
}}

function row(p) {{
  const gh = p.github_url ? `<a href="https://${{p.github_url}}" target="_blank">${{esc(p.github_url)}}</a>` : `<span class="none">-</span>`;
  return `<tr>
    <td>${{esc(p.topic)}}</td>
    <td>${{chip(p.venue, p.venue==='arXiv'?'v-arxiv':'')}}</td>
    <td>${{esc(p.year||'')}}</td>
    <td class="t-title">${{esc(p.title)}}</td>
    <td class="t-authors">${{esc(p.authors||'')}}</td>
    <td class="t-orgs">${{esc(p.organizations||'')}}</td>
    <td class="t-problem">${{esc(p.target_problem||'')}}</td>
    <td class="t-core">${{esc(p.core_idea||'')}}</td>
    <td>${{gh}}</td>
    <td>${{figCell(p)}}</td>
  </tr>`;
}}

function render() {{
  const t = document.getElementById('f-topic').value;
  const v = document.getElementById('f-venue').value;
  const y = document.getElementById('f-year').value;
  const q = document.getElementById('f-q').value.toLowerCase().trim();
  let out = [];
  for (const p of PAPERS) {{
    if (t && p.topic !== t) continue;
    if (v && p.venue !== v) continue;
    if (y && String(p.year) !== y) continue;
    if (q) {{
      const hay = [p.title,p.authors,p.organizations,p.target_problem,p.core_idea,p.abstract]
        .filter(x=>x).join(' ').toLowerCase();
      if (!hay.includes(q)) continue;
    }}
    out.push(p);
  }}
  document.getElementById('rows').innerHTML = out.map(row).join('');
  document.getElementById('stat').textContent = out.length + ' / ' + PAPERS.length + ' papers';
}}

function openImg(src) {{
  const b = document.getElementById('modal-body');
  b.innerHTML = `<img src="${{src}}" alt="">`;
  document.getElementById('modal').classList.add('on');
}}
function openMermaid(code) {{
  const b = document.getElementById('modal-body');
  const id = 'm' + Date.now();
  b.innerHTML = `<div class="mermaid" id="${{id}}">${{esc(code)}}</div>`;
  document.getElementById('modal').classList.add('on');
  mermaid.run({{querySelector:'#'+id, postRenderCallback:null}}).catch(()=>{{}});
}}
function closeModal() {{ document.getElementById('modal').classList.remove('on'); }}
document.getElementById('modal').addEventListener('click', e => {{
  if (e.target.id === 'modal') closeModal();
}});
['f-topic','f-venue','f-year','f-q'].forEach(id =>
  document.getElementById(id).addEventListener('input', render));
render();
</script>
</body>
</html>
"""
