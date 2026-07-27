"""PaperLab command-line interface."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
from pathlib import Path

from . import pipeline
from . import schedule as schedule_mod
from . import web as web_mod
from .config import Config, load_config
from .store import Store


def _load_env(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not match:
            continue
        key, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _log(message: str) -> None:
    print(message, flush=True)


# === 1. UPDATE: incremental cataloging ===
def cmd_update(args, cfg: Config, store: Store) -> None:
    lock_path = cfg.root / "data/update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit("another paperlab_local update is already running") from exc
        _log("== incremental catalog search ==")
        pipeline.search_catalog(
            store, cfg, full=args.full,
            enrich_limit=args.enrich_limit, log=_log)
        _log("== enrich metadata (abstracts, DOIs, links) ==")
        if not args.no_enrich:
            pipeline.enrich_openalex_dois(store, limit=args.enrich_limit, log=_log)
            pipeline.enrich_semantic_scholar(store, limit=args.enrich_limit, log=_log)
        _log("update complete")


# === 2. SEARCH: semantic LLM-based search ===
def cmd_search(args, cfg: Config, store: Store) -> None:
    """Search for papers matching a user description using LLM."""
    from . import llm
    description = args.description
    _log(f"Searching for: {description}")
    # Strategy: keyword pre-filter from DB, then LLM semantic match
    # 1. Extract search keywords from user description
    try:
        search_kws = llm.extract_keywords(
            "User search query", description, model=llm.get_model())
        _log(f"Extracted search keywords: {search_kws}")
    except Exception as exc:
        _log(f"Keyword extraction failed ({exc}), using raw text search")
        search_kws = [description.lower()]

    # 2. Gather candidates: papers matching any keyword in title/abstract/keywords
    candidates = []
    seen_ids = set()
    for kw in search_kws:
        rows = store.search_by_keywords(kw, limit=100)
        for r in rows:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                candidates.append({
                    "id": r["id"],
                    "title": r["title"],
                    "keywords": r["keywords"] or "",
                    "abstract": r["abstract"] or "",
                    "venue": r["venue"],
                    "year": r["year"],
                })
    _log(f"Found {len(candidates)} candidate papers from keyword pre-filter")

    # If too few candidates, broaden with raw text search
    if len(candidates) < 10:
        rows = store.search_by_keywords(description, limit=100)
        for r in rows:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                candidates.append({
                    "id": r["id"], "title": r["title"],
                    "keywords": r["keywords"] or "",
                    "abstract": r["abstract"] or "",
                    "venue": r["venue"], "year": r["year"],
                })
        _log(f"Extended to {len(candidates)} candidates")

    if not candidates:
        _log("No matching papers found.")
        return

    # 3. For candidates without keywords, extract them via LLM (lazy)
    needs_kw = [c for c in candidates if not c["keywords"]]
    if needs_kw:
        _log(f"Extracting keywords for {len(needs_kw)} un-keyworded papers...")
        extracted = 0
        for c in needs_kw[:50]:  # cap to avoid excessive LLM calls
            try:
                kws = llm.extract_keywords(c["title"], c["abstract"],
                                           model=llm.get_model())
                if kws:
                    store.set_keywords(c["id"], kws)
                    c["keywords"] = ",".join(kws)
                    extracted += 1
            except Exception:
                pass
        _log(f"Extracted keywords for {extracted} papers")

    # 4. LLM semantic matching
    try:
        matches = llm.semantic_search(description, candidates[:200],
                                       model=llm.get_model())
    except Exception as exc:
        _log(f"LLM semantic match failed ({exc}), falling back to keyword match")
        matches = [{"id": c["id"], "score": 0.7 if any(
            k in (c["keywords"] + " " + c["title"] + " " + c["abstract"]).lower()
            for k in search_kws) else 0.3, "reason": "keyword match"}
            for c in candidates[:30]]

    # 5. Print results
    _log(f"\n=== {len(matches)} matching papers ===")
    for m in sorted(matches, key=lambda x: -x.get("score", 0)):
        row = store.conn.execute(
            "SELECT title, venue, year, arxiv_id, pdf_path FROM papers WHERE id=?",
            (m["id"],)).fetchone()
        if not row:
            continue
        score = m.get("score", 0)
        reason = m.get("reason", "")
        pdf_mark = "[PDF]" if row["pdf_path"] else ""
        _log(f"  [{score:.2f}] {row['venue']} {row['year']} {pdf_mark} {row['title']}")
        if reason:
            _log(f"         {reason}")
        if row["arxiv_id"]:
            _log(f"         arXiv: {row['arxiv_id']}")


# === 3. DOWNLOAD: single paper or by search ===
def cmd_download(args, cfg: Config, store: Store) -> None:
    """Download a specific paper PDF."""
    paper_ref = args.paper
    # Resolve paper by id, title, or arxiv_id
    row = None
    # Try exact id
    row = store.conn.execute("SELECT * FROM papers WHERE id=?", (paper_ref,)).fetchone()
    if not row:
        # Try arxiv_id
        row = store.conn.execute(
            "SELECT * FROM papers WHERE arxiv_id=?", (paper_ref,)).fetchone()
    if not row:
        # Try title match
        rows = store.conn.execute(
            "SELECT * FROM papers WHERE lower(title) LIKE ?",
            (f"%{paper_ref.lower()}%",)).fetchall()
        if len(rows) == 1:
            row = rows[0]
        elif len(rows) > 1:
            _log(f"Multiple papers match '{paper_ref}':")
            for i, r in enumerate(rows[:10]):
                _log(f"  [{i}] {r['venue']} {r['year']} {r['title']}")
            return
    if not row:
        _log(f"Paper not found: {paper_ref}")
        return

    _log(f"Downloading: {row['title']}")
    count = pipeline.download_single(store, cfg, row["id"], log=_log)
    _log(f"Download {'succeeded' if count else 'failed'}")


def cmd_report(args, cfg: Config, store: Store) -> None:
    """Generate (or force-regenerate) a deep-read report for a paper."""
    from . import report as report_mod
    paper_ref = args.paper
    row = store.conn.execute("SELECT * FROM papers WHERE id=?", (paper_ref,)).fetchone()
    if not row:
        row = store.conn.execute(
            "SELECT * FROM papers WHERE arxiv_id=?", (paper_ref,)).fetchone()
    if not row:
        rows = store.conn.execute(
            "SELECT * FROM papers WHERE lower(title) LIKE ?",
            (f"%{paper_ref.lower()}%",)).fetchall()
        if len(rows) == 1:
            row = rows[0]
        elif len(rows) > 1:
            _log(f"Multiple papers match '{paper_ref}':")
            for i, r in enumerate(rows[:10]):
                _log(f"  [{i}] {r['venue']} {r['year']} {r['title']}")
            return
    if not row:
        _log(f"Paper not found: {paper_ref}")
        return
    existing = store.get_report(row["id"])
    if existing and not args.force:
        _log(f"Report already exists: {existing['report_path']}")
        _log("Use --force to regenerate.")
        return
    _log(f"Generating report: {row['title']}")
    path = report_mod.generate_report(store, cfg, row["id"], log=_log)
    _log(f"Report saved: {path}")


# === Other commands ===
def cmd_enrich(args, cfg: Config, store: Store) -> None:
    bulk = pipeline.enrich_openalex_dois(
        store, limit=args.limit, batch_size=args.batch_size, log=_log)
    s2 = pipeline.enrich_semantic_scholar(store, limit=args.s2_limit, log=_log)
    mcp = 0
    if not args.bulk_only:
        mcp = pipeline.enrich_metadata(store, cfg, limit=args.mcp_limit, log=_log)
    _log(f"metadata enriched: openalex={bulk}, s2={s2}, mcp={mcp}")


def cmd_keywords(args, cfg: Config, store: Store) -> None:
    """Manage keywords for a paper."""
    if args.action == "add":
        kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
        store.add_keywords(args.paper_id, kws)
        _log(f"Added keywords to {args.paper_id}: {kws}")
    elif args.action == "set":
        kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
        store.set_keywords(args.paper_id, kws)
        _log(f"Set keywords for {args.paper_id}: {kws}")
    elif args.action == "show":
        kws = store.get_keywords(args.paper_id)
        _log(f"Keywords: {kws}")
    elif args.action == "batch":
        """Extract keywords for all papers missing them."""
        from . import llm
        rows = store.papers_without_keywords(limit=args.limit)
        _log(f"Extracting keywords for {len(rows)} papers...")
        done = 0
        for i, r in enumerate(rows, 1):
            try:
                kws = llm.extract_keywords(r["title"], r["abstract"],
                                           model=llm.get_model())
                if kws:
                    store.set_keywords(r["id"], kws)
                    done += 1
                    if i % 50 == 0:
                        _log(f"  {i}/{len(rows)}: extracted={done}")
            except Exception as exc:
                _log(f"  {i}/{len(rows)}: failed ({exc})")
        _log(f"Done: {done}/{len(rows)} papers got keywords")


def cmd_web(args, cfg: Config, store: Store) -> None:
    if args.action == "install":
        path = schedule_mod.install_web(cfg, args.config, host=args.host, port=args.port)
        _log(f"Web LaunchAgent installed: {path}")
    elif args.action == "remove":
        _log("Web LaunchAgent removed" if schedule_mod.remove_web() else "not installed")
    elif args.action == "status":
        _log(json.dumps(schedule_mod.web_status(), ensure_ascii=False))
    else:
        web_mod.serve(cfg.db_path, host=args.host, port=args.port, config_path=args.config)


def cmd_stats(args, cfg: Config, store: Store) -> None:
    _log(f"[all] {store.stats()}")
    kw_count = store.conn.execute(
        "SELECT COUNT(*) FROM papers WHERE keywords IS NOT NULL AND keywords!=''").fetchone()[0]
    _log(f"[keywords] {kw_count} papers have keywords")


def cmd_venues(args, cfg: Config, store: Store) -> None:
    from . import venues
    for venue in venues.supported():
        _log(venue)


def cmd_schedule(args, cfg: Config, store: Store) -> None:
    if args.action == "install":
        path = schedule_mod.install(cfg, args.config)
        _log(f"LaunchAgent installed: {path}")
    elif args.action == "remove":
        _log("removed" if schedule_mod.remove() else "not installed")
    else:
        _log(json.dumps(schedule_mod.status(), ensure_ascii=False))


def cmd_sync(args, cfg: Config, store: Store | None) -> None:
    if args.action == "now":
        from .incremental import sync_now
        sync_now(cfg, log=_log)
        return
    from . import sync as sync_mod
    if args.action == "status":
        sync_mod.status(cfg, log=_log)
    elif args.action == "push":
        sync_mod.push(cfg, dry_run=args.dry_run, force=args.force, log=_log)
    elif args.action == "pull":
        sync_mod.pull(cfg, include_pdfs=args.all, force=args.force, log=_log)
    elif args.action == "gc":
        sync_mod.garbage_collect(cfg, apply=args.apply, log=_log)


def cmd_auth(args, cfg: Config, store: Store | None) -> None:
    if args.action == "hash-password":
        import getpass
        from .auth import hash_password
        first = getpass.getpass("Admin password: ")
        second = getpass.getpass("Confirm password: ")
        if not first or first != second:
            raise SystemExit("passwords do not match or are empty")
        print(hash_password(first))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paperlab_local", description="AI conference paper database")
    _default_config = str(Path(__file__).resolve().parent.parent / "topics.yaml")
    parser.add_argument("--config", "-c", default=_default_config, help="path to config file")
    parser.add_argument("--db", default=None, help="override SQLite path")
    sub = parser.add_subparsers(dest="cmd", required=False)

    # 1. update
    update = sub.add_parser("update", help="incrementally catalogue recent AI conference papers")
    update.add_argument("--full", action="store_true", help="scan full 5-year range")
    update.add_argument("--enrich-limit", type=int, default=None)
    update.add_argument("--no-enrich", action="store_true", help="skip metadata enrichment")
    update.set_defaults(func=cmd_update)

    # 2. search
    search = sub.add_parser("search", help="search papers by description using LLM")
    search.add_argument("description", help="natural language description of what you are looking for")
    search.set_defaults(func=cmd_search)

    # 3. download
    download = sub.add_parser("download", help="download a paper PDF by id/title/arxiv_id")
    download.add_argument("paper", help="paper id, arxiv_id, or title substring")
    download.set_defaults(func=cmd_download)
    

    # report
    report = sub.add_parser("report", help="generate a deep-read report for a paper")
    report.add_argument("paper", help="paper id, arxiv_id, or title substring")
    report.add_argument("--force", action="store_true", help="regenerate even if report exists")
    report.set_defaults(func=cmd_report)

    # enrich
    enrich = sub.add_parser("enrich", help="fill abstracts and metadata")
    enrich.add_argument("--limit", type=int, default=None)
    enrich.add_argument("--batch-size", type=int, default=25)
    enrich.add_argument("--bulk-only", action="store_true")
    enrich.add_argument("--mcp-limit", type=int, default=None)
    enrich.add_argument("--s2-limit", type=int, default=None)
    enrich.set_defaults(func=cmd_enrich)

    # keywords
    kw = sub.add_parser("keywords", help="manage paper keywords")
    kw_sub = kw.add_subparsers(dest="kw_action", required=True)
    kw_add = kw_sub.add_parser("add", help="add keywords to a paper")
    kw_add.add_argument("paper_id")
    kw_add.add_argument("keywords", help="comma-separated keywords")
    kw_add.set_defaults(func=cmd_keywords, action="add")
    kw_set = kw_sub.add_parser("set", help="overwrite keywords for a paper")
    kw_set.add_argument("paper_id")
    kw_set.add_argument("keywords", help="comma-separated keywords")
    kw_set.set_defaults(func=cmd_keywords, action="set")
    kw_show = kw_sub.add_parser("show", help="show keywords for a paper")
    kw_show.add_argument("paper_id")
    kw_show.set_defaults(func=cmd_keywords, action="show")
    kw_batch = kw_sub.add_parser("batch", help="extract keywords for papers missing them")
    kw_batch.add_argument("--limit", type=int, default=None)
    kw_batch.set_defaults(func=cmd_keywords, action="batch")

    # web
    web = sub.add_parser("web", help="serve/manage the Web UI")
    web.add_argument("action", nargs="?", default="serve",
                     choices=("serve", "install", "status", "remove"))
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.set_defaults(func=cmd_web)

    # stats
    stats = sub.add_parser("stats")
    stats.set_defaults(func=cmd_stats)

    venues_p = sub.add_parser("venues")
    venues_p.set_defaults(func=cmd_venues)

    schedule = sub.add_parser("schedule")
    schedule.add_argument("action", choices=("install", "status", "remove"))
    schedule.set_defaults(func=cmd_schedule)

    sync_p = sub.add_parser("sync", help="synchronize runtime data with private S3/R2 storage")
    sync_sub = sync_p.add_subparsers(dest="sync_action", required=True)
    sync_status = sync_sub.add_parser("status", help="compare local and remote revisions")
    sync_status.set_defaults(func=cmd_sync, action="status")
    sync_push = sync_sub.add_parser("push", help="upload a consistent database snapshot and changed files")
    sync_push.add_argument("--dry-run", action="store_true")
    sync_push.add_argument("--force", action="store_true", help="replace an unrelated remote revision")
    sync_push.set_defaults(func=cmd_sync, action="push")
    sync_pull = sync_sub.add_parser("pull", help="restore the latest database, reports and reviews")
    sync_pull.add_argument("--all", action="store_true", help="also download all PDFs")
    sync_pull.add_argument("--force", action="store_true", help="replace an unlinked local database after backup")
    sync_pull.set_defaults(func=cmd_sync, action="pull")
    sync_gc = sync_sub.add_parser("gc", help="find unreferenced remote content objects")
    sync_gc.add_argument("--apply", action="store_true", help="delete unreferenced objects")
    sync_gc.set_defaults(func=cmd_sync, action="gc")

    sync_now = sync_sub.add_parser("now", help="bidirectionally synchronize journal changes")
    sync_now.set_defaults(func=cmd_sync, action="now")

    auth_p = sub.add_parser("auth", help="authentication utilities")
    auth_sub = auth_p.add_subparsers(dest="auth_action", required=True)
    auth_hash = auth_sub.add_parser("hash-password", help="create an Argon2id admin password hash")
    auth_hash.set_defaults(func=cmd_auth, action="hash-password")

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config_file = Path(args.config).resolve()
    _load_env(config_file.parent / ".env")
    cfg = load_config(args.config)
    db_path = args.db or str(cfg.db_path)
    if not args.cmd:
        # No subcommand: default to serving the web UI
        web_mod.serve(cfg.db_path, host="127.0.0.1", port=8765, config_path=args.config)
        return 0
    if args.cmd in {"sync", "auth"}:
        args.func(args, cfg, None)
        return 0
    if args.cmd in ("web", "schedule") and getattr(args, "action", "") in {"install", "status", "remove"}:
        args.func(args, cfg, None)
        return 0
    with Store(db_path) as store:
        args.func(args, cfg, store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
