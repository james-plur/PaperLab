"""Export local public data and reports to the decoupled Cloudflare site."""
from __future__ import annotations
import html, json, re, shutil, subprocess, time
from pathlib import Path

def slug(v): return str(v).replace("/", "_").replace(":", "-")
def sql(v):
    if v is None: return "NULL"
    if isinstance(v, (int, float)): return str(v)
    return "'" + str(v).replace("'", "''") + "'"
def bigrams(v):
    return " ".join(s[i:i+2] for s in re.findall(r"[\u3400-\u9fff]+", v or "") for i in range(max(1,len(s)-1)))
def report_html(md, title):
    out=[]
    for line in md.splitlines():
        x=html.escape(line)
        x=re.sub(r"!\[([^]]*)\]\(([^)]+)\)",r'<img alt="\1" src="\2">',x)
        x=re.sub(r"\*\*([^*]+)\*\*",r"<strong>\1</strong>",x)
        m=re.match(r"^(#{1,6})\s+(.+)$",x)
        out.append(f"<h{len(m[1])}>{m[2]}</h{len(m[1])}>" if m else (f"<p>{x}</p>" if x else ""))
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><link rel="stylesheet" href="/styles.css"><style>.report-page{{max-width:920px;margin:auto;padding:36px 24px;line-height:1.75}}.report-page img{{max-width:100%}}</style></head><body><main class="report-page"><a href="/">← All papers</a>{''.join(out)}</main></body></html>'''
def run(cmd,cwd): subprocess.run(cmd,cwd=cwd,check=True)

def publish(store,cfg,repo,limit=40000,dry_run=False,log=print):
    repo=Path(repo).expanduser().resolve()
    if not (repo/"wrangler.toml").is_file(): raise RuntimeError(f"invalid public repo: {repo}")
    state_path=cfg.root/"data/publish-state.json"
    try: state=json.loads(state_path.read_text())
    except (OSError,ValueError): state={"ts":0,"id":"","report_mtime":0}
    rows=store.conn.execute('''SELECT p.*,(SELECT group_concat(v.venue||' '||v.year,'|') FROM paper_venues v WHERE v.paper_id=p.id) memberships,(SELECT landing_url FROM paper_sources s WHERE s.paper_id=p.id AND landing_url!='' ORDER BY last_seen DESC LIMIT 1) source_fallback,(SELECT s.pdf_url FROM paper_sources s WHERE s.paper_id=p.id AND s.pdf_url!='' ORDER BY last_seen DESC LIMIT 1) pdf_fallback,r.report_path,r.status report_status FROM papers p LEFT JOIN paper_reports r ON r.paper_id=p.id WHERE coalesce(p.last_updated,p.discovered_at,0)>? OR (coalesce(p.last_updated,p.discovered_at,0)=? AND p.id>?) ORDER BY coalesce(p.last_updated,p.discovered_at,0),p.id LIMIT ?''',(state["ts"],state["ts"],state["id"],limit)).fetchall()
    work=repo/".publish-data"; work.mkdir(exist_ok=True)
    lines=[]
    for r in rows:
        text=" ".join(str(r[k] or "") for k in ("title","authors","organizations","abstract","keywords"))
        rp=f"reports/{slug(r['id'])}/index.html" if r["report_status"]=="ok" and r["report_path"] and Path(r["report_path"]).is_file() else None
        vals=[r["id"],r["title"],r["authors"],r["organizations"],r["awards"],r["venue"],r["year"],r["abstract"],r["doi"],r["arxiv_id"],r["source_url"] or r["source_fallback"],r["pdf_url"] or r["pdf_fallback"],r["citation_count"],r["keywords"],r["memberships"],r["topics"],rp,bigrams(text),r["last_updated"] or r["discovered_at"] or 0]
        lines.append("INSERT OR REPLACE INTO papers VALUES("+",".join(sql(v) for v in vals)+");")
    export=work/"upsert.sql"; export.write_text("\n".join(lines))
    reports=0; newest=state["report_mtime"]
    for r in store.conn.execute("SELECT p.id,p.title,r.report_path FROM paper_reports r JOIN papers p ON p.id=r.paper_id WHERE r.status='ok'"):
        src=Path(r["report_path"])
        if not src.is_file() or src.stat().st_mtime<=state["report_mtime"]: continue
        s=slug(r["id"]); dest=repo/"public/reports"/s; dest.mkdir(parents=True,exist_ok=True)
        md=src.read_text().replace(f"figures/{s}/","figures/"); (dest/"index.html").write_text(report_html(md,r["title"]))
        figs=src.parent/"figures"/s
        if figs.is_dir(): shutil.copytree(figs,dest/"figures",dirs_exist_ok=True)
        newest=max(newest,src.stat().st_mtime); reports+=1
    log(f"prepared papers={len(rows)} reports={reports}")
    if dry_run:return {"papers":len(rows),"reports":reports,"dry_run":True}
    if rows: run(["npx","wrangler","d1","execute","ai-top-paper-lab","--remote","--yes",f"--file={export}"],repo)
    run(["git","add","public/reports"],repo)
    if subprocess.run(["git","diff","--cached","--quiet"],cwd=repo).returncode:
        run(["git","commit","-m",f"Publish {reports} updated reports"],repo); run(["git","push"],repo)
    run(["npx","wrangler","pages","deploy","public","--project-name","ai-top-paper-lab","--branch","master"],repo)
    if rows: state.update(ts=rows[-1]["last_updated"] or rows[-1]["discovered_at"] or 0,id=rows[-1]["id"])
    state.update(report_mtime=newest,published_at=time.time()); state_path.write_text(json.dumps(state,indent=2))
    return {"papers":len(rows),"reports":reports,"deployed":True,"more":len(rows)==limit}
