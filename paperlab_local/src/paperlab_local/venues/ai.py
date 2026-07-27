"""AI/ML/NLP/CV conference catalogue adapters.

Official proceedings are used where their archive URL is stable.  DBLP volume
pages are the uniform fallback and, unlike its search API, are not capped at
1,000 hits.  Only inproceedings records are returned.
"""
from __future__ import annotations

import html
import json
import re
from urllib.parse import urlencode, urljoin

from ..http import fetch_text
from .systems import parse_dblp


AI_VENUES = {
    "NEURIPS": "nips",
    "ICML": "icml",
    "ICLR": "iclr",
    "AAAI": "aaai",
    "IJCAI": "ijcai",
    "KDD": "kdd",
    "MLSYS": "mlsys",
    "ACL": "acl",
    "EMNLP": "emnlp",
    "NAACL": "naacl",
    "CVPR": "cvpr",
    "ICCV": "iccv",
    "ECCV": "eccv",
}

DBLP_STEMS = {"NEURIPS": "neurips"}
# ECCV proceedings are published as many numbered LNCS volumes and DBLP does
# not provide an aggregate ``eccvYYYY.html`` page.  Recent editions can exceed
# thirty volumes, so the old 12-volume ceiling silently truncated the venue.
DBLP_SPLIT_VENUES = {"ECCV"}
DBLP_MAX_VOLUMES = 64


def _get(url: str) -> str:
    try:
        # A full scan touches over one hundred venue/year pages.  One flaky TLS
        # handshake must not block the whole catalogue for several minutes;
        # failed years are persisted as pending and retried by the next run.
        return fetch_text(url, compressed=True, max_time=20, retries=0)
    except RuntimeError:
        return ""


def _official_neurips(year: int) -> list[dict]:
    url = f"https://papers.nips.cc/paper_files/paper/{year}"
    raw = _get(url)
    out = []
    blocks = re.findall(r'<li class="conference"[^>]*>(.*?)</li>', raw, re.S | re.I)
    for block in blocks:
        link = re.search(r'<a[^>]+title="paper title"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                         block, re.S | re.I)
        if not link:
            continue
        href, title = link.groups()
        title = html.unescape(re.sub(r"<[^>]+>", " ", title))
        title = re.sub(r"\s+", " ", title).strip()
        authors_match = re.search(r'<span class="paper-authors">(.*?)</span>', block, re.S | re.I)
        authors = ""
        if authors_match:
            authors = html.unescape(re.sub(r"<[^>]+>", " ", authors_match.group(1)))
            authors = re.sub(r"\s+", " ", authors).strip()
        if title:
            out.append({"title": title, "authors": authors,
                        "source_url": "https://papers.nips.cc" + href,
                        "source": "neurips-proceedings"})
    return out


def _official_cvf(venue: str, year: int) -> list[dict]:
    url = f"https://openaccess.thecvf.com/{venue}{year}?day=all"
    raw = _get(url)
    out = []
    # CVF uses <dt class="ptitle"><br><a href="...">title</a></dt>
    blocks = re.findall(r'<dt class="ptitle">(.*?)</dt>\s*<dd>(.*?)</dd>', raw, re.S | re.I)
    for title_block, authors_block in blocks:
        link = re.search(r'<a href="([^"]+)">(.*?)</a>', title_block, re.S | re.I)
        if not link:
            continue
        title = html.unescape(re.sub(r"<[^>]+>", " ", link.group(2)))
        title = re.sub(r"\s+", " ", title).strip()
        authors = html.unescape(re.sub(r"<[^>]+>", " ", authors_block))
        authors = re.sub(r"\s+", " ", authors).strip()
        href = link.group(1)
        out.append({"title": title, "authors": authors,
                    "source_url": "https://openaccess.thecvf.com" + href,
                    "source": "cvf"})
    return out


def _parse_eccv_virtual(raw: str, year: int) -> list[dict]:
    try:
        records = (json.loads(raw) or {}).get("results", [])
    except (ValueError, TypeError, AttributeError):
        return []
    output = []
    seen: set[str] = set()
    for record in records:
        title = str(record.get("name") or "").strip()
        if not title:
            continue
        key = re.sub(r"\W+", " ", title.lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        author_rows = record.get("authors") or []
        authors = [str(author.get("fullname") or "").strip() for author in author_rows]
        organizations = []
        for author in author_rows:
            institution = str(author.get("institution") or "").strip()
            if institution and institution not in organizations:
                organizations.append(institution)
        virtual = record.get("virtualsite_url") or ""
        output.append({
            "title": title,
            "authors": "; ".join(name for name in authors if name),
            "organizations": "; ".join(organizations),
            "abstract": record.get("abstract") or "",
            "source_id": str(record.get("uid") or record.get("id") or ""),
            "source_url": urljoin("https://eccv.ecva.net", virtual),
            "pdf_url": record.get("paper_pdf_url") or "",
            "track": record.get("event_type") or record.get("eventtype") or "",
            "source": "eccv-official",
        })
    return output


def _parse_ecva_archive(raw: str, year: int) -> list[dict]:
    section = re.search(
        rf"<!--\s*ECCV\s+{year}\s*-->(.*?)(?=<!--\s*ECCV\s+\d{{4}}\s*-->|$)",
        raw, re.S | re.I,
    )
    if not section:
        return []
    blocks = re.findall(
        r'<dt class="ptitle"[^>]*>.*?<a\s+href=["\']?([^"\' >]+)["\']?[^>]*>'
        r'(.*?)</a>\s*</dt>\s*<dd>(.*?)</dd>\s*<dd>(.*?)</dd>',
        section.group(1), re.S | re.I,
    )
    output = []
    for detail_href, title_raw, authors_raw, links_raw in blocks:
        title = html.unescape(re.sub(r"<[^>]+>", " ", title_raw))
        title = re.sub(r"\s+", " ", title).strip()
        authors = html.unescape(re.sub(r"<[^>]+>", " ", authors_raw))
        authors = re.sub(r"\s+", " ", authors).strip()
        pdf_match = re.search(r'href=["\']([^"\']+\.pdf)["\']', links_raw, re.I)
        doi_match = re.search(r'href=["\']https?://(?:dx\.)?doi\.org/([^"\']+)["\']',
                              links_raw, re.I)
        if not doi_match:
            doi_match = re.search(
                r'href=["\']https?://link\.springer\.com/chapter/([^"\']+)["\']',
                links_raw, re.I,
            )
        output.append({
            "title": title,
            "authors": authors,
            "doi": html.unescape(doi_match.group(1)) if doi_match else "",
            "source_url": urljoin("https://www.ecva.net/", detail_href),
            "pdf_url": urljoin("https://www.ecva.net/", pdf_match.group(1)) if pdf_match else "",
            "source": "ecva-archive",
        })
    return output


def _official_eccv(year: int) -> list[dict]:
    if year == 2024:
        raw = _get(
            "https://eccv.ecva.net/static/virtual/data/eccv-2024-orals-posters.json"
        )
        papers = _parse_eccv_virtual(raw, year)
        if papers:
            return papers
    archive = _get("https://www.ecva.net/papers.php")
    return _parse_ecva_archive(archive, year)


def _dblp_pages(venue: str, year: int) -> list[dict]:
    slug = AI_VENUES[venue]
    stem = DBLP_STEMS.get(venue, slug)
    base = f"https://dblp.org/db/conf/{slug}/{stem}{year}"
    base_raw = _get(base + ".html")
    # Most DBLP conference-year pages aggregate every proceedings volume.  Use
    # that page alone when present; suffix probing is reserved for venues that
    # publish only split volume pages.
    if (venue not in DBLP_SPLIT_VENUES and base_raw
            and "entry inproceedings" in base_raw):
        output = parse_dblp(base_raw)
        for paper in output:
            paper["source"] = "dblp"
            paper["source_url"] = base + ".html"
        return output
    urls = [base + f"-{volume}.html" for volume in range(1, DBLP_MAX_VOLUMES + 1)]
    output: list[dict] = []
    seen: set[str] = set()
    misses = 0
    for index, url in enumerate(urls):
        raw = _get(url)
        if not raw or "entry inproceedings" not in raw:
            misses += 1
            # After a valid base/volume series, three misses mean the series ended.
            if misses >= 3:
                break
            continue
        misses = 0
        for paper in parse_dblp(raw):
            key = paper["title"].strip().lower()
            if key in seen:
                continue
            seen.add(key)
            paper["source"] = "dblp"
            paper["source_url"] = url
            output.append(paper)
    return output


def _dblp_stream(venue: str, year: int) -> list[dict]:
    """Page through a DBLP venue stream without the 1,000-hit truncation.

    This is essential for ECCV: its LNCS parts do not always form one stable,
    contiguous URL series, while the DBLP stream is the authoritative logical
    conference collection.
    """
    slug = AI_VENUES[venue]
    offset = 0
    page_size = 1000
    output: list[dict] = []
    seen: set[str] = set()
    while True:
        query = f"stream:streams/conf/{slug}: year:{year}"
        params = urlencode({"q": query, "h": page_size, "f": offset, "format": "json"})
        raw = _get("https://dblp.org/search/publ/api?" + params)
        if not raw:
            return []
        try:
            result = json.loads(raw).get("result", {})
            hits = result.get("hits", {})
            records = hits.get("hit", []) or []
            total = int(hits.get("@total", 0) or 0)
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
            return []
        for record in records:
            info = record.get("info", {})
            title = html.unescape(re.sub(r"<[^>]+>", " ", str(info.get("title") or "")))
            title = re.sub(r"\s+", " ", title).strip().rstrip(".")
            if not title:
                continue
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            author_value = (info.get("authors") or {}).get("author", [])
            if not isinstance(author_value, list):
                author_value = [author_value]
            authors = []
            for author in author_value:
                name = author.get("text") if isinstance(author, dict) else author
                if name:
                    authors.append(str(name))
            ee = info.get("ee") or ""
            if isinstance(ee, list):
                ee = next((str(value) for value in ee if value), "")
            doi = str(info.get("doi") or "")
            if not doi and "doi.org/" in str(ee):
                doi = str(ee).split("doi.org/", 1)[1]
            output.append({
                "title": title,
                "authors": ", ".join(authors),
                "doi": doi,
                "source_url": str(info.get("url") or ee),
                "pdf_url": str(ee) if str(ee).lower().endswith(".pdf") else "",
                "source_id": str(info.get("key") or ""),
                "source": "dblp",
            })
        offset += len(records)
        if not records or offset >= total:
            break
    return output


def fetch(venue: str, year: int) -> list[dict]:
    venue = venue.upper()
    if venue not in AI_VENUES:
        raise KeyError(f"unsupported AI venue: {venue}")
    papers: list[dict] = []
    if venue == "ECCV":
        papers = _official_eccv(year)
        if not papers:
            papers = _dblp_stream(venue, year)
    elif venue == "NEURIPS":
        papers = _official_neurips(year)
    elif venue in {"CVPR", "ICCV"}:
        papers = _official_cvf(venue, year)
    if not papers:
        papers = _dblp_pages(venue, year)
    return [{**paper, "venue": venue, "year": year} for paper in papers]
