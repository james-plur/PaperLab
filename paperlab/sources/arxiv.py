"""arXiv Atom API search by keywords + categories, filtered by year range."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

from ..http import fetch_text

ATOM = "{http://www.w3.org/2005/Atom}"


def _query(keywords: list[str], categories: list[str]) -> str:
    parts = []
    if keywords:
        parts.append("(" + " OR ".join(f'all:"{k}"' for k in keywords) + ")")
    if categories:
        parts.append("(" + " OR ".join(f"cat:{c}" for c in categories) + ")")
    base = " AND ".join(parts) if parts else "all:computer science"
    # Placeholder timestamps are replaced with the caller's year window below.
    return f"({base}) AND submittedDate:[YYYY01010000 TO ZZZZ12312359]"


def search_arxiv(keywords: list[str], categories: list[str], *,
                 year_range: tuple[int, int], max_results: int = 200) -> list[dict]:
    """Return candidate dicts from arXiv within year_range."""
    q = _query(keywords, categories).replace("YYYY", f"{year_range[0]}").replace("ZZZZ", f"{year_range[1]}")
    url = (
        "http://export.arxiv.org/api/query?search_query=" + quote(q, safe=":")
        + f"&start=0&max_results={max_results}"
        + "&sortBy=submittedDate&sortOrder=descending"
    )
    raw = fetch_text(url, max_time=60)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise RuntimeError(f"arXiv parse error: {e}") from e
    out = []
    for entry in root.findall(f"{ATOM}entry"):
        id_url = (entry.findtext(f"{ATOM}id") or "").strip()
        m = re.search(r"/abs/(\d{4}\.\d{4,5})(?:v\d+)?", id_url)
        arxiv_id = m.group(1) if m else None
        title = " ".join((entry.findtext(f"{ATOM}title") or "").split())
        summary = " ".join((entry.findtext(f"{ATOM}summary") or "").split())
        published = (entry.findtext(f"{ATOM}published") or "")[:10]
        year = None
        try:
            year = int(published[:4]) if published else None
        except ValueError:
            year = None
        authors = [
            (a.findtext(f"{ATOM}name") or "").strip()
            for a in entry.findall(f"{ATOM}author")
        ]
        authors = [a for a in authors if a]
        if not title or not arxiv_id:
            continue
        out.append({
            "title": title,
            "authors": ", ".join(authors),
            "abstract": summary,
            "arxiv_id": arxiv_id,
            "year": year,
            "venue": "arXiv",
        })
    return out
