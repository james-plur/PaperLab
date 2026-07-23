"""Incremental catalogue, enrichment, scoring, download and extraction stages."""
from __future__ import annotations

import hashlib
import shutil
from datetime import date
from pathlib import Path

from . import extract as extract_mod
from . import figure as figure_mod
from . import pdf as pdf_mod
from . import relevance as rel_mod
from . import venues
from .config import Config, TopicConfig
from .mcp_client import MCPError, PaperSearchMCP
from .sources import arxiv, semantic_scholar as s2
from .sources import scrape as scrape_mod
from .sources.affiliations import openalex_affiliations
from .sources.openalex_bulk import fetch_dois, normalize_doi
from .sources.paper_search import enrich_title, title_similarity
from .store import Store


def _years(topic: TopicConfig) -> list[int]:
    return list(range(topic.year_range[0], topic.year_range[1] + 1))


def _id_slug(paper_id: str) -> str:
    return paper_id.replace("/", "_").replace(":", "-")


def _catalog_years(cfg: Config, *, full: bool) -> list[int]:
    start, end = cfg.catalog.year_range
    if full:
        return list(range(start, end + 1))
    current = min(date.today().year, end)
    first = max(start, current - cfg.catalog.incremental_lookback_years)
    return list(range(first, current + 1))


def search_catalog(store: Store, cfg: Config, *, full: bool = False,
                   selected_venues: list[str] | None = None,
                   selected_years: list[int] | None = None,
                   enrich_limit: int | None = None, log=print) -> dict:
    """Collect complete venue/year catalogues, then enrich incomplete rows via MCP."""
    venue_names = [v.upper() for v in (selected_venues or cfg.catalog.venues)]
    years = selected_years or _catalog_years(cfg, full=full)
    collected = 0
    for venue in venue_names:
        for year in years:
            try:
                candidates = venues.fetch(venue, year)
            except Exception as exc:
                store.record_venue_sync(venue, year, status="error", error=str(exc))
                log(f"  [catalog] {venue} {year}: failed ({exc})")
                continue
            if not candidates:
                existing = store.conn.execute(
                    "SELECT count(*) FROM papers WHERE venue=? AND year=?",
                    (venue, year),
                ).fetchone()[0]
                if existing:
                    store.record_venue_sync(
                        venue, year, status="ok", count=existing,
                        error="latest refresh empty; retained existing catalogue",
                    )
                    log(f"  [catalog] {venue} {year}: refresh empty; retained {existing} papers")
                else:
                    store.record_venue_sync(venue, year, status="pending", count=0,
                                            error="proceedings unavailable or empty")
                    log(f"  [catalog] {venue} {year}: pending/empty")
                continue
            titles = sorted(str(c.get("title") or "") for c in candidates)
            checksum = hashlib.sha256("\n".join(titles).encode()).hexdigest()[:16]
            for candidate in candidates:
                candidate.setdefault("source", f"catalog:{venue.lower()}")
                store.upsert_candidate(candidate, commit=False)
            store.conn.commit()
            collected += len(candidates)
            log(f"  [catalog] {venue} {year}: {len(candidates)} papers")
    enrich_openalex_dois(store, limit=enrich_limit, log=log)
    # S2 fills abstracts directly via the Graph API, independent of the MCP
    # server, so new ACM/IEEE DOIs and legacy DOI-less imports both get abstracts.
    s2_filled = enrich_semantic_scholar(store, limit=enrich_limit, log=log)
    enriched = enrich_metadata(store, cfg, limit=enrich_limit, log=log)
    return {"collected": collected, "enriched": enriched, "s2": s2_filled}
 

def enrich_metadata(store: Store, cfg: Config, *, limit: int | None = None, log=print) -> int:
    rows = store.pending_metadata(limit=limit)
    if not rows:
        return 0
    done = 0
    try:
        client = PaperSearchMCP(cfg.mcp.command, cfg.mcp.args, timeout=cfg.mcp.timeout_seconds)
        client.start()
    except Exception as exc:
        log(f"  [mcp] startup failed ({exc}); metadata remains pending")
        return 0
    try:
        # DOI-backed OpenAlex enrichment is handled in batches before this
        # stage.  DBLP and Crossref do not normally provide abstracts, so the
        # expensive title fallback focuses on the two sources that do.
        mcp_sources = [source for source in cfg.mcp.sources
                       if source in {"arxiv", "semantic"}]
        if not mcp_sources:
            mcp_sources = cfg.mcp.sources
        for index, row in enumerate(rows, 1):
            try:
                fields, matches = enrich_title(
                    client, row["title"], sources=mcp_sources, year=row["year"])
                if fields:
                    if not fields.get("venue"):
                        fields.pop("venue", None)
                    if not row["organizations"]:
                        organizations = openalex_affiliations(
                            doi=fields.get("doi") or row["doi"] or "", title=row["title"])
                        if organizations:
                            fields["organizations"] = "; ".join(organizations)
                    store.set_enrichment(row["id"], fields)
                    for match in matches:
                        store.upsert_source(
                            row["id"], source=match["source"], source_id=match["source_id"],
                            landing_url=match.get("source_url"), pdf_url=match.get("pdf_url"),
                            raw=match.get("raw"),
                        )
                    store.conn.commit()
                    done += 1
                else:
                    store.set_enrichment(row["id"], {"metadata_status": "not_found"})
                store.upsert_source(
                    row["id"], source="paper-search-mcp-attempt", source_id="",
                    raw={"sources": mcp_sources, "matched": bool(fields)},
                )
                store.conn.commit()
                if index % 5 == 0:
                    log(f"  [mcp] {index}/{len(rows)} metadata lookups")
            except Exception as exc:
                log(f"  [mcp] {row['id']}: enrichment failed ({exc})")
    finally:
        client.close()
    return done


def enrich_openalex_dois(store: Store, *, limit: int | None = None,
                         batch_size: int = 25, log=print) -> int:
    """Bulk-fill abstracts, affiliations and OA links for DOI-backed rows."""
    sql = """SELECT * FROM papers
             WHERE doi IS NOT NULL AND trim(doi)!=''
               AND (abstract IS NULL OR trim(abstract)=''
                    OR organizations IS NULL OR trim(organizations)='')
               AND NOT EXISTS (
                   SELECT 1 FROM paper_sources ps
                   WHERE ps.paper_id=papers.id
                     AND ps.source IN ('openalex','openalex-miss'))
             ORDER BY year DESC, discovered_at"""
    params: tuple = ()
    if limit is not None and limit > 0:
        sql += " LIMIT ?"
        params = (limit,)
    rows = store.conn.execute(sql, params).fetchall()
    done = 0
    for start in range(0, len(rows), batch_size):
        chunk = rows[start:start + batch_size]
        try:
            matches = fetch_dois([row["doi"] for row in chunk])
        except Exception as exc:
            log(f"  [openalex-bulk] batch {start // batch_size + 1} failed ({exc})")
            continue
        for row in chunk:
            match = matches.get(normalize_doi(row["doi"]))
            if not match:
                store.upsert_source(
                    row["id"], source="openalex-miss", source_id=normalize_doi(row["doi"]),
                    raw={"doi": normalize_doi(row["doi"]), "status": "not_found"},
                )
                continue
            store.set_enrichment(row["id"], match, commit=False)
            store.upsert_source(
                row["id"], source="openalex", source_id=match["source_id"],
                landing_url=match.get("source_url"), pdf_url=match.get("pdf_url"),
                raw={"doi": match.get("doi"), "title": match.get("title")},
            )
            done += 1
        store.conn.commit()
        log(f"  [openalex-bulk] {min(start + batch_size, len(rows))}/{len(rows)} "
            f"DOIs, matched={done}")
    return done


def enrich_semantic_scholar(store: Store, *, limit: int | None = None,
                            log=print) -> int:
    """Fill abstracts, arxiv_id and citations via the Semantic Scholar Graph API.

    Unlike ``enrich_metadata``, this path talks directly to the S2 HTTP API and
    does not require the paper-search-mcp server.  It is the reliable fallback
    for catalogue papers that DBLP/OpenAlex could not give an abstract -- common
    for brand-new ACM/IEEE DOIs and for legacy imports that lack a DOI entirely.
    """
    sql = """SELECT * FROM papers
             WHERE (metadata_status!='complete'
                    OR abstract IS NULL OR trim(abstract)='')
               AND NOT EXISTS (
                   SELECT 1 FROM paper_sources ps
                   WHERE ps.paper_id=papers.id
                     AND ps.source IN ('semantic-scholar','semantic-scholar-miss'))
             ORDER BY year DESC, discovered_at"""
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    rows = store.conn.execute(sql, params).fetchall()
    if not rows:
        return 0
    done = 0
    # ---- Phase 1: batch DOI lookups (up to 500 per request) ---------------
    # Papers with DOIs go through POST /paper/batch, which resolves hundreds
    # of records in a single HTTP call instead of one GET per paper.
    doi_rows = [r for r in rows if (r["doi"] or "").strip()]
    no_doi_rows = [r for r in rows if not (r["doi"] or "").strip()]
    # Rows whose DOI lookup missed (or was skipped due to a transient batch
    # error) still get a title-match fallback, matching the pre-batch logic.
    title_fallback_rows: list = list(no_doi_rows)
    batch_size = 500
    for start in range(0, len(doi_rows), batch_size):
        batch = doi_rows[start:start + batch_size]
        ids = [f"DOI:{_normalize_s2_doi(r['doi'])}" for r in batch]
        try:
            results = s2.get_batch_strict(ids)
        except s2.TransientError:
            # Leave the entire batch retryable -- do not record misses.
            log(f"  [s2] batch {start // batch_size + 1} transient error; "
                f"{len(batch)} rows will be retried next run")
            continue
        for row, s2_id in zip(batch, ids):
            result = results.get(s2_id)
            if result:
                _apply_s2_result(store, row, result)
                done += 1
            else:
                # DOI not in S2 (yet) -- try title match before giving up.
                title_fallback_rows.append(row)
        store.conn.commit()
        log(f"  [s2] batch {start // batch_size + 1}: "
            f"{min(start + batch_size, len(doi_rows))}/{len(doi_rows)} DOI lookups, "
            f"filled={done}")
    # ---- Phase 2: title-match fallback ------------------------------------
    for index, row in enumerate(title_fallback_rows, 1):
        title = row["title"]
        doi = row["doi"] or ""
        transient = False
        try:
            result = s2.match_by_title_strict(title)
        except s2.TransientError:
            transient = True
            result = None
        # Guard against same-name / near-miss collisions: S2 search/match
        # returns the closest hit, which can still be a different paper.
        if result and title_similarity(title, result.get("title") or "") < 0.90:
            result = None
        if transient:
            store.conn.commit()
            if index % 25 == 0:
                log(f"  [s2] title {index}/{len(title_fallback_rows)}, filled={done} "
                    f"(transient on '{title[:40]}')")
            continue
        if not result:
            store.upsert_source(
                row["id"], source="semantic-scholar-miss", source_id=doi,
                raw={"title": title, "doi": doi, "year": row["year"]})
            store.conn.commit()
            if index % 25 == 0:
                log(f"  [s2] title {index}/{len(title_fallback_rows)}, filled={done}")
            continue
        _apply_s2_result(store, row, result)
        done += 1
        if index % 25 == 0:
            log(f"  [s2] title {index}/{len(title_fallback_rows)}, filled={done}")
    return done


def enrich_scrape(store: Store, *, limit: int | None = None, log=print) -> int:
    """Fill abstracts by scraping proceedings pages (NeurIPS, PMLR, ACL, CVF).

    Uses ``source_url`` stored in ``paper_sources.raw_json`` to fetch the
    publisher's page directly.  Complements API-based enrichment and is
    especially useful when OpenAlex / S2 are rate-limited.
    """
    import json
    sql = """SELECT p.*, ps.raw_json, ps.source AS ps_source
             FROM papers p
             JOIN paper_sources ps ON ps.paper_id = p.id
             WHERE (p.abstract IS NULL OR trim(p.abstract)='')
               AND ps.raw_json IS NOT NULL
               AND (ps.raw_json LIKE '%papers.nips.cc%'
                    OR ps.raw_json LIKE '%aclanthology.org%'
                    OR ps.raw_json LIKE '%proceedings.mlr.press%')
               AND NOT EXISTS (
                   SELECT 1 FROM paper_sources ps2
                   WHERE ps2.paper_id = p.id
                     AND ps2.source IN ('scrape','scrape-miss'))
             ORDER BY p.year DESC"""
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    rows = store.conn.execute(sql, params).fetchall()
    if not rows:
        return 0
    done = 0
    for index, row in enumerate(rows, 1):
        try:
            raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
        except (ValueError, TypeError):
            continue
        url = raw.get("source_url", "")
        if not url:
            continue
        try:
            abstract = scrape_mod.scrape(url)
        except Exception:
            abstract = None
        if abstract and len(abstract) > 30:
            store.set_enrichment(
                row["id"], {"abstract": abstract, "metadata_status": "complete"},
                commit=False)
            store.upsert_source(
                row["id"], source="scrape", source_id=url, raw={"url": url})
            store.conn.commit()
            done += 1
        else:
            store.upsert_source(
                row["id"], source="scrape-miss", source_id=url,
                raw={"url": url, "title": row["title"]})
            store.conn.commit()
        if index % 50 == 0:
            log(f"  [scrape] {index}/{len(rows)}: filled={done}")
    return done


def _normalize_s2_doi(doi: str) -> str:
    """Strip URL prefixes and lowercase a DOI for S2 API calls."""
    cleaned = (doi or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    return cleaned


def _apply_s2_result(store: Store, row, result: dict) -> None:
    """Write a single S2 enrichment result to the store."""
    fields = {
        key: result[key] for key in ("abstract", "arxiv_id", "doi", "authors",
                                     "citation_count", "pdf_url", "year")
        if result.get(key) not in (None, "", 0)
    }
    fields["metadata_status"] = "complete" if fields.get("abstract") else "partial"
    store.set_enrichment(row["id"], fields, commit=False)
    store.upsert_source(
        row["id"], source="semantic-scholar",
        source_id=str(result.get("doi") or result.get("arxiv_id") or ""),
        landing_url=result.get("source_url") or result.get("pdf_url"),
        pdf_url=result.get("pdf_url"), raw=result,
    )
    store.conn.commit()


def score_topics(store: Store, cfg: Config, *, topic_names: list[str] | None = None,
                 force: bool = False, log=print) -> int:
    topics = [cfg.topic(name) for name in topic_names] if topic_names else list(cfg.topics)
    pending_by_paper: dict[str, set[str]] = {}
    rows_by_id = {}
    hashes = {topic.name: rel_mod.definition_hash(topic) for topic in topics}
    for topic in topics:
        for row in store.pending_topic(topic.name, hashes[topic.name], rel_mod.PROMPT_VERSION,
                                       force=force):
            rows_by_id[row["id"]] = row
            pending_by_paper.setdefault(row["id"], set()).add(topic.name)
    rows = [rows_by_id[paper_id] for paper_id in pending_by_paper]
    if not rows:
        return 0
    topic_map = {topic.name: topic for topic in topics}
    done = 0
    batch_size = 25
    for start in range(0, len(rows), batch_size):
        chunk = rows[start:start + batch_size]
        needed_names = sorted({name for row in chunk for name in pending_by_paper[row["id"]]})
        needed_topics = [topic_map[name] for name in needed_names]
        try:
            results = rel_mod.score_multi_batch(chunk, needed_topics, model=cfg.models.scorer)
        except Exception as exc:
            log(f"  [score] batch {start // batch_size + 1} failed ({exc}); left pending")
            continue
        for row in chunk:
            for topic_id in pending_by_paper[row["id"]]:
                result = (results.get(row["id"]) or {}).get(topic_id)
                if result is None:
                    continue
                topic = topic_map[topic_id]
                score_value, reason = result
                store.set_topic_score(
                    row["id"], topic_id, score_value, reason,
                    threshold=topic.relevance_threshold, model=cfg.models.scorer,
                    definition_hash=hashes[topic_id], prompt_version=rel_mod.PROMPT_VERSION,
                )
                done += 1
        log(f"  [score] papers {min(start + batch_size, len(rows))}/{len(rows)}")
    return done


def _valid_pdf(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 1000 and path.read_bytes()[:5] == b"%PDF-"
    except OSError:
        return False


def download_topic(store: Store, cfg: Config, topic: TopicConfig, *,
                   min_score: float | None = None, limit: int | None = None,
                   retry_errors: bool = False, allow_scihub: bool = False,
                   scihub_base_url: str = "", log=print) -> int:
    if not store.has_topic_scores(topic.name):
        raise ValueError(f"topic '{topic.name}' has no scores; run paperlab score --topic {topic.name} first")
    threshold = topic.relevance_threshold if min_score is None else min_score
    rows = store.topic_papers_v2(topic.name, min_score=threshold, relevant_only=False)
    rows = [row for row in rows if row["pdf_status"] is None or
            (retry_errors and row["pdf_status"] in {"error", "unavailable"})]
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        return 0
    cfg.pdf_root.joinpath("pdfs").mkdir(parents=True, exist_ok=True)
    client: PaperSearchMCP | None = None
    downloaded = 0
    for row in rows:
        destination = cfg.pdf_root / "pdfs" / f"{_id_slug(row['id'])}.pdf"
        if _valid_pdf(destination):
            store.set_pdf(row["id"], path=str(destination), status="ok")
            downloaded += 1
            continue
        url = row["pdf_url"]
        if row["arxiv_id"]:
            url = f"https://arxiv.org/pdf/{row['arxiv_id']}.pdf"
        if url:
            try:
                pdf_mod.download(url, destination)
                if not _valid_pdf(destination):
                    raise RuntimeError("response is not a valid PDF")
                store.set_pdf(row["id"], path=str(destination), status="ok", pdf_url=url)
                downloaded += 1
                log(f"  [pdf] {row['id']}: downloaded")
                continue
            except Exception as exc:
                log(f"  [pdf] {row['id']}: direct download failed ({exc}); trying OA fallback")
        try:
            if client is None:
                client = PaperSearchMCP(cfg.mcp.command, cfg.mcp.args, timeout=cfg.mcp.timeout_seconds)
                client.start()
            source_row = store.conn.execute(
                """SELECT source,source_id FROM paper_sources WHERE paper_id=?
                   AND source IN ('arxiv','semantic','crossref','pmc','core','europepmc',
                                  'citeseerx','doaj','base','zenodo','hal','ssrn')
                   ORDER BY CASE source WHEN 'arxiv' THEN 0 WHEN 'semantic' THEN 1 ELSE 2 END LIMIT 1""",
                (row["id"],),
            ).fetchone()
            source = source_row["source"] if source_row else ("arxiv" if row["arxiv_id"] else "crossref")
            source_id = source_row["source_id"] if source_row else (row["arxiv_id"] or row["doi"] or row["id"])
            result = client.download_with_fallback(
                source=source, paper_id=source_id, save_path=destination.parent,
                doi=row["doi"] or "", title=row["title"], use_scihub=allow_scihub,
                scihub_base_url=scihub_base_url,
            )
            resolved = Path(result).expanduser()
            if resolved.exists() and resolved.resolve() != destination.resolve():
                shutil.move(str(resolved), destination)
            if not _valid_pdf(destination):
                raise RuntimeError(result or "no OA PDF found")
            store.set_pdf(row["id"], path=str(destination), status="ok")
            downloaded += 1
        except Exception as exc:
            store.set_pdf(row["id"], path=None, status="unavailable")
            log(f"  [pdf] {row['id']}: unavailable ({exc})")
    if client:
        client.close()
    return downloaded


# ---- v1 compatibility -------------------------------------------------

def collect(store: Store, cfg: Config, topic: TopicConfig, *, log=print) -> int:
    count = 0
    for venue in topic.venues:
        for year in _years(topic):
            try:
                candidates = venues.fetch(venue, year)
            except Exception as exc:
                log(f"  [venue] {venue} {year}: fetch failed ({exc})")
                continue
            for candidate in candidates:
                store.upsert_candidate(candidate, topic.name)
                count += 1
    if topic.keywords or topic.arxiv_categories:
        try:
            candidates = arxiv.search_arxiv(topic.keywords, topic.arxiv_categories,
                                            year_range=topic.year_range, max_results=200)
        except Exception as exc:
            log(f"  [arxiv] search failed ({exc})")
            candidates = []
        for candidate in candidates:
            store.upsert_candidate(candidate, topic.name)
            count += 1
    return count


def score(store: Store, cfg: Config, topic: TopicConfig, *, log=print) -> int:
    # Once v2 catalogue data exists, use independent topic scores.
    return score_topics(store, cfg, topic_names=[topic.name], log=log)


def download(store: Store, cfg: Config, topic: TopicConfig, *, log=print) -> int:
    if store.has_topic_scores(topic.name):
        return download_topic(store, cfg, topic, log=log)
    # Legacy behavior for old tests/configurations.
    rows = [row for row in store.pending(topic.name, "download")
            if (row["relevance_score"] or 0) >= topic.relevance_threshold]
    count = 0
    for row in rows:
        arxiv_id, pdf_url = row["arxiv_id"], row["pdf_url"]
        if not arxiv_id and not pdf_url:
            info = s2.match_by_title(row["title"]) or {}
            if info:
                store.set_enrichment(row["id"], info)
                arxiv_id, pdf_url = info.get("arxiv_id"), info.get("pdf_url")
        if arxiv_id:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        if not pdf_url:
            store.set_pdf(row["id"], path=None, status="unavailable")
            continue
        destination = topic.pdf_dir / f"{_id_slug(row['id'])}.pdf"
        try:
            pdf_mod.download(pdf_url, destination)
            store.set_pdf(row["id"], path=str(destination), status="ok")
            count += 1
        except Exception:
            store.set_pdf(row["id"], path=None, status="error")
    return count


def extract(store: Store, cfg: Config, topic: TopicConfig, *, log=print) -> int:
    rows = store.pending(topic.name, "extract")
    figure_dir = cfg.figures_dir(topic.name)
    count = 0
    for row in rows:
        pdf_path = Path(row["pdf_path"])
        try:
            ext = extract_mod.extract_paper(row["title"], row["authors"], row["abstract"],
                                            pdf_path, model=cfg.models.extractor)
        except Exception as exc:
            log(f"  [extract] {row['id']}: LLM failed ({exc})")
            continue
        github = pdf_mod.find_github_url(pdf_path, title=row["title"])
        kind, figure_path, mermaid = None, None, None
        destination = figure_dir / f"{_id_slug(row['id'])}.png"
        try:
            if figure_mod.detect_from_pdf(pdf_path, destination):
                kind, figure_path = "pdf", str(destination)
        except Exception:
            pass
        if not kind:
            try:
                mermaid = figure_mod.generate_mermaid(
                    row["title"], ext.get("target_problem", ""), ext.get("core_idea", ""),
                    model=cfg.models.figure)
                if mermaid:
                    kind = "mermaid"
            except Exception:
                pass
        store.set_extraction(
            row["id"], target_problem=ext.get("target_problem"), core_idea=ext.get("core_idea"),
            organizations=ext.get("organizations"), github_url=github, figure_kind=kind,
            figure_path=figure_path, figure_mermaid=mermaid)
        count += 1
    return count


def download_single(store: Store, cfg: Config, paper_id: str, *, log=print) -> int:
    """Download a single paper PDF. Tries direct URL, arXiv term search, USENIX proceedings."""
    row = store.conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
    if not row:
        log("  [pdf] paper not found: " + paper_id)
        return 0
    destination = cfg.pdf_root / "pdfs" / (_id_slug(row['id']) + ".pdf")
    if _valid_pdf(destination):
        store.set_pdf(row["id"], path=str(destination), status="ok")
        return 1
    # 1. arXiv by known arxiv_id
    if row["arxiv_id"]:
        aid = row["arxiv_id"].strip()
        if "/" in aid:
            aid = aid.rsplit("/", 1)[-1]
        url = "https://arxiv.org/pdf/" + aid + ".pdf"
        try:
            pdf_mod.download(url, destination)
            if _valid_pdf(destination):
                store.set_pdf(row["id"], path=str(destination), status="ok", pdf_url=url)
                return 1
        except Exception:
            pass
        import time as _t; _t.sleep(3)
    # 2. Direct pdf_url
    url = row["pdf_url"]
    if url and "pdf" in url.lower():
        try:
            pdf_mod.download(url, destination)
            if _valid_pdf(destination):
                store.set_pdf(row["id"], path=str(destination), status="ok", pdf_url=url)
                return 1
        except Exception:
            pass
    # 3. arXiv term search (works for most system papers with preprints)
    try:
        from .sources.paper_search import title_similarity
        from .http import fetch_text
        import xml.etree.ElementTree as ET
        import re as _re
        from urllib.parse import quote
        ATOM = "{http://www.w3.org/2005/Atom}"
        words = [w.strip(".,;:!?\"'()[]{}") for w in row["title"].split() if len(w) > 2][:6]
        query = " ".join(words)
        arxiv_url = ("http://export.arxiv.org/api/query?search_query=all:" + quote(query)
                     + "&start=0&max_results=5&sortBy=relevance")
        raw = fetch_text(arxiv_url, max_time=30)
        root = ET.fromstring(raw)
        for entry in root.findall(ATOM + "entry"):
            id_url = (entry.findtext(ATOM + "id") or "").strip()
            m = _re.search(r"/abs/(\d{4}\.\d{4,5})", id_url)
            aid = m.group(1) if m else None
            et = " ".join((entry.findtext(ATOM + "title") or "").split())
            if aid and title_similarity(row["title"], et) >= 0.85:
                pdf_url = "https://arxiv.org/pdf/" + aid + ".pdf"
                pdf_mod.download(pdf_url, destination)
                if _valid_pdf(destination):
                    store.set_enrichment(row["id"], {"arxiv_id": aid})
                    store.set_pdf(row["id"], path=str(destination), status="ok", pdf_url=pdf_url)
                    return 1
                import time as _t; _t.sleep(3)
    except Exception:
        pass
    # 4. USENIX proceedings (OSDI, NSDI, FAST)
    venue = row["venue"] or ""
    if venue in ("OSDI", "NSDI", "FAST") and row["year"]:
        try:
            from .http import fetch_text
            yy = str(row["year"] % 100).zfill(2)
            proc_url = "https://www.usenix.org/conference/" + venue.lower() + yy + "/technical-sessions"
            html = fetch_text(proc_url, max_time=30, compressed=False)
            for m in re.finditer(r'href="(/system/files/[^"]+\.pdf)"[^>]*>([^<]+)', html):
                link_title = m.group(2).strip()
                if title_similarity(row["title"], link_title) >= 0.7:
                    pdf_url = "https://www.usenix.org" + m.group(1)
                    pdf_mod.download(pdf_url, destination)
                    if _valid_pdf(destination):
                        store.set_pdf(row["id"], path=str(destination), status="ok", pdf_url=pdf_url)
                        return 1
        except Exception:
            pass
    log("  [pdf] all download methods failed for: " + row["title"])
    return 0
