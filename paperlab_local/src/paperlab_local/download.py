"""Per-publisher PDF download strategies.

Download order for each paper:
1. USENIX (OSDI/NSDI/FAST): scrape citation_pdf_url from presentation page
2. ACM DL (ISCA/MICRO/ASPLOS/SIGCOMM/SOSP): construct dl.acm.org/doi/pdf/X
3. IEEE (HPCA/INFOCOM): DOI redirect -> stamp.jsp -> PDF
4. arXiv: all-field term search -> title match -> arxiv.org/pdf/X
5. Direct pdf_url if available
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from .http import fetch_text, UA
from .sources.paper_search import title_similarity

ATOM = "{http://www.w3.org/2005/Atom}"


def _is_pdf(data: bytes) -> bool:
    return len(data) > 1000 and data[:5] == b"%PDF-"


def _download_url(url: str, dest: Path, headers: dict | None = None) -> bool:
    """Download URL to dest, return True if valid PDF."""
    hdrs = headers or {"User-Agent": UA, "Accept": "application/pdf,*/*"}
    try:
        r = requests.get(url, headers=hdrs, timeout=120, allow_redirects=True, stream=True)
        if r.status_code == 200:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    if chunk:
                        f.write(chunk)
            return _is_pdf(dest.read_bytes())
    except Exception:
        pass
    return False


# ── 1. USENIX (OSDI, NSDI, FAST) ──────────────────────────────
def download_usenix(source_url: str, dest: Path) -> str | None:
    """Download PDF from USENIX presentation page via citation_pdf_url meta tag.

    URL pattern: https://www.usenix.org/conference/{venue}{YY}/presentation/{slug}
    PDF pattern: https://www.usenix.org/system/files/{venue}{YY}-{slug}.pdf
    """
    if "usenix.org" not in (source_url or ""):
        return None
    try:
        r = requests.get(source_url, headers={"User-Agent": UA, "Accept-Encoding": "identity"},
                        timeout=30)
        if r.status_code != 200:
            return None
        # Method 1: citation_pdf_url meta tag (most reliable)
        m = re.search(r'citation_pdf_url"\s+content="([^"]+)"', r.text)
        if m:
            pdf_url = m.group(1)
            if _download_url(pdf_url, dest):
                return pdf_url
        # Method 2: construct URL from presentation slug
        # /conference/osdi25/presentation/huang-songlin -> /system/files/osdi25-huang-songlin.pdf
        m2 = re.search(r"/conference/(\w+)/presentation/([\w-]+)", source_url)
        if m2:
            venue_slug, paper_slug = m2.group(1), m2.group(2)
            constructed = f"https://www.usenix.org/system/files/{venue_slug}-{paper_slug}.pdf"
            if _download_url(constructed, dest):
                return constructed
    except Exception:
        pass
    return None


# ── 2. ACM Digital Library (ISCA, MICRO, ASPLOS, SIGCOMM, SOSP) ──
def download_acm(doi: str, dest: Path) -> str | None:
    """Download PDF from ACM Digital Library via dl.acm.org/doi/pdf/{doi}.

    ACM DL returns 403 to bots without proper headers, but some papers are OA.
    """
    if not doi:
        return None
    clean_doi = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if clean_doi.startswith(prefix):
            clean_doi = clean_doi[len(prefix):]
    if not clean_doi.startswith("10."):
        return None
    # Try dl.acm.org/doi/pdf/{doi}
    url = f"https://dl.acm.org/doi/pdf/{clean_doi}"
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/pdf,text/html,*/*",
    }
    if _download_url(url, dest, hdrs):
        return url
    return None


# ── 3. IEEE Xplore (HPCA, INFOCOM) ────────────────────────────
def download_ieee(doi: str, dest: Path) -> str | None:
    """Download PDF from IEEE Xplore via DOI redirect -> stamp.jsp.

    IEEE returns 202 (bot challenge) for direct DOI access, but the stamp.jsp
    endpoint sometimes serves the PDF directly.
    """
    if not doi:
        return None
    clean_doi = doi.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if clean_doi.lower().startswith(prefix):
            clean_doi = clean_doi[len(prefix):]
    try:
        # Follow DOI to IEEE page
        r = requests.get(f"https://doi.org/{clean_doi}",
                        headers={"User-Agent": UA}, timeout=30, allow_redirects=True)
        if r.status_code == 200 and r.text:
            # Find arnumber from IEEE page
            m = re.search(r'"arnumber":"?(\d+)"?', r.text)
            if not m:
                m = re.search(r'document/(\d+)', r.url)
            if m:
                arnum = m.group(1)
                # Try stamp.jsp which serves the PDF
                stamp_url = f"https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber={arnum}"
                if _download_url(stamp_url, dest):
                    return stamp_url
                # Try direct PDF URL
                pdf_url = f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={arnum}"
                if _download_url(pdf_url, dest):
                    return pdf_url
    except Exception:
        pass
    return None


# ── 3b. Unpaywall (works for ACM open-access papers) ──────────
def download_unpaywall(doi: str, dest: Path) -> str | None:
    """Download PDF via Unpaywall API (free, keyless).

    Unpaywall knows about OA copies even when direct ACM/IEEE access returns 403.
    """
    if not doi:
        return None
    clean = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if clean.startswith(prefix):
            clean = clean[len(prefix):]
    if not clean.startswith("10."):
        return None
    try:
        import json
        r = requests.get(
            f"https://api.unpaywall.org/v2/{clean}?email=paperlab_local@localhost",
            timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("is_oa"):
            return None
        best = data.get("best_oa_location") or {}
        pdf_url = best.get("url_for_pdf") or best.get("url")
        if pdf_url and _download_url(pdf_url, dest):
            return pdf_url
    except Exception:
        pass
    return None


# ── 4. arXiv (universal fallback for all venues) ──────────────
def download_arxiv_search(title: str, dest: Path) -> tuple[str | None, str | None]:
    """Search arXiv by title keywords, download if match found.

    Returns (pdf_url, arxiv_id) or (None, None).
    """
    if not title:
        return None, None
    words = [w.strip(".,;:!?'\"()[]{}") for w in title.split() if len(w) > 2][:6]
    if not words:
        return None, None
    query = quote(" ".join(words))
    url = (f"http://export.arxiv.org/api/query?search_query=all:{query}"
           f"&start=0&max_results=5&sortBy=relevance")
    try:
        raw = fetch_text(url, max_time=30)
        root = ET.fromstring(raw)
        for entry in root.findall(f"{ATOM}entry"):
            et = " ".join((entry.findtext(f"{ATOM}title") or "").split())
            id_url = (entry.findtext(f"{ATOM}id") or "").strip()
            m = re.search(r"/abs/(\d{4}\.\d{4,5})", id_url)
            aid = m.group(1) if m else None
            if aid and title_similarity(title, et) >= 0.85:
                pdf_url = f"https://arxiv.org/pdf/{aid}.pdf"
                if _download_url(pdf_url, dest):
                    return pdf_url, aid
                time.sleep(3)
    except Exception:
        pass
    return None, None


# ── Main download dispatcher ──────────────────────────────────
def download_paper(paper: dict, dest: Path, *, log=print) -> str | None:
    """Try all download strategies for a paper. Returns pdf_url or None.

    Strategies (in order of reliability):
    1. arXiv by known arxiv_id (instant if available)
    2. USENIX citation_pdf_url (100% for OSDI/NSDI/FAST)
    3. arXiv title search (works for ~50% of system papers with preprints)
    4. ACM DL with retry + Unpaywall (intermittent 403, retry helps)
    5. IEEE stamp.jsp + Unpaywall
    6. Direct pdf_url
    7. Unpaywall for any DOI
    """
    import time as _time
    venue = (paper.get("venue") or "").upper()
    title = paper.get("title") or ""
    source_url = paper.get("source_url") or ""
    doi = paper.get("doi") or ""
    arxiv_id = paper.get("arxiv_id") or ""
    pdf_url = paper.get("pdf_url") or ""

    # 0. Already downloaded
    if dest.is_file() and dest.stat().st_size > 1000 and _is_pdf(dest.read_bytes()[:10]):
        return "already"

    # 1. arXiv by known arxiv_id
    if arxiv_id:
        aid = arxiv_id.strip()
        if "/" in aid: aid = aid.rsplit("/", 1)[-1]
        url = f"https://arxiv.org/pdf/{aid}.pdf"
        if _download_url(url, dest):
            log(f"  [ok] arXiv direct: {aid}")
            return url
        _time.sleep(3)

    # 2. USENIX (OSDI/NSDI/FAST) - 100% success rate
    if venue in ("OSDI", "NSDI", "FAST") and "usenix.org" in source_url:
        result = download_usenix(source_url, dest)
        if result:
            log(f"  [ok] USENIX: {source_url}")
            return result

    # 3. arXiv title search (universal, works for many papers)
    result, aid = download_arxiv_search(title, dest)
    if result:
        log(f"  [ok] arXiv search: {aid}")
        return result

    # 4. ACM DL with retry (intermittent 403, sometimes works on 2nd try)
    if doi.startswith("10.1145"):
        for attempt in range(3):
            result = download_acm(doi, dest)
            if result:
                log(f"  [ok] ACM DL: {doi} (attempt {attempt+1})")
                return result
            result = download_unpaywall(doi, dest)
            if result:
                log(f"  [ok] Unpaywall: {doi}")
                return result
            _time.sleep(2)

    # 5. IEEE
    if doi.startswith("10.1109"):
        result = download_ieee(doi, dest)
        if result:
            log(f"  [ok] IEEE: {doi}")
            return result
        result = download_unpaywall(doi, dest)
        if result:
            log(f"  [ok] Unpaywall: {doi}")
            return result

    # 6. Direct pdf_url
    if pdf_url and ("pdf" in pdf_url.lower() or "arxiv" in pdf_url.lower()):
        if _download_url(pdf_url, dest):
            log(f"  [ok] direct URL")
            return pdf_url

    # 7. Unpaywall for any DOI
    if doi:
        result = download_unpaywall(doi, dest)
        if result:
            log(f"  [ok] Unpaywall fallback: {doi}")
            return result

    log(f"  [FAIL] {title[:50]}")
    return None



# ── Parallel batch download ────────────────────────────────
def download_batch_parallel(papers: list[dict], pdf_root: Path, *,
                            workers: int = 8, db_path: Path = None, log=print) -> dict:
    """Download multiple papers in parallel using a thread pool.

    papers: list of dicts with id, title, source_url, doi, arxiv_id, pdf_url, venue
    pdf_root: directory to save PDFs (pdf_root/pdfs/<slug>.pdf)
    Returns: {"ok": int, "fail": int, "results": {paper_id: pdf_url or None}}
    """
    import concurrent.futures
    import threading

    _db_path = db_path or Path("data/papers.db")
    pdf_dir = pdf_root / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    results_lock = threading.Lock()
    ok_count = [0]
    fail_count = [0]

    def _slug(pid):
        return pid.replace("/", "_").replace(":", "-")

    def _download_one(paper):
        pid = paper["id"]
        dest = pdf_dir / (_slug(pid) + ".pdf")
        # Skip if already valid
        if dest.is_file() and dest.stat().st_size > 1000:
            with open(dest, "rb") as f:
                if _is_pdf(f.read(10)):
                    # Update DB
                    import sqlite3 as _sq, time as _t
                    try:
                        c = _sq.connect(str(_db_path), timeout=10)
                        c.execute("UPDATE papers SET pdf_path=?, pdf_status='ok' WHERE id=?",
                                  (str(dest), pid))
                        c.commit()
                        c.close()
                    except Exception:
                        pass
                    with results_lock:
                        results[pid] = "already"
                        ok_count[0] += 1
                    return
        result = download_paper(paper, dest, log=lambda m: None)
        with results_lock:
            if result:
                results[pid] = result
                ok_count[0] += 1
                # Update DB
                import sqlite3 as _sq, time as _t
                try:
                    c = _sq.connect(str(_db_path), timeout=10)
                    c.execute("UPDATE papers SET pdf_path=?, pdf_status='ok', pdf_url=?, last_updated=? WHERE id=?",
                              (str(dest), result, _t.time(), pid))
                    c.commit()
                    c.close()
                except Exception:
                    pass
            else:
                results[pid] = None
                fail_count[0] += 1
                # Mark as error in DB
                import sqlite3 as _sq
                try:
                    c = _sq.connect(str(_db_path), timeout=10)
                    c.execute("UPDATE papers SET pdf_status='error' WHERE id=?", (pid,))
                    c.commit()
                    c.close()
                except Exception:
                    pass

    papers_to_download = []
    for p in papers:
        pid = p["id"]
        dest = pdf_dir / (_slug(pid) + ".pdf")
        if dest.is_file() and dest.stat().st_size > 1000:
            with open(dest, "rb") as f:
                if _is_pdf(f.read(10)):
                    results[pid] = "already"
                    ok_count[0] += 1
                    continue
        papers_to_download.append(p)

    log(f"[parallel] {len(papers_to_download)} to download, {workers} workers")
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_download_one, p): p for p in papers_to_download}
        for future in concurrent.futures.as_completed(futures):
            done += 1
            if done % 50 == 0:
                with results_lock:
                    log(f"  [parallel] {done}/{len(papers_to_download)}: ok={ok_count[0]} fail={fail_count[0]}")

    log(f"[parallel] done: ok={ok_count[0]} fail={fail_count[0]}")
    return {"ok": ok_count[0], "fail": fail_count[0], "results": results}
