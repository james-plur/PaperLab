"""Semantic Scholar Graph API: title match + arXiv lookup for enrichment.

Used to (a) get citation counts for arXiv candidates and (b) resolve an
open-access PDF / arxiv_id / DOI for venue candidates that lack them.
"""
from __future__ import annotations

import json
from urllib.parse import quote

from ..http import fetch_text

BASE = "https://api.semanticscholar.org/graph/v1/paper"
FIELDS = "title,abstract,year,externalIds,citationCount,openAccessPdf,authors"


def _get(url: str) -> dict | None:
    try:
        raw = fetch_text(url, max_time=30)
    except RuntimeError:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


class TransientError(RuntimeError):
    """Raised when S2 returned a retryable condition (429 / 5xx / network)."""


def _get_strict(url: str) -> dict | None:
    """Like ``_get`` but distinguishes a real miss (None) from a transient error.

    Returns the parsed dict on success, ``None`` when S2 confirms there is no
    matching paper, and raises :class:`TransientError` for 429/5xx/network
    failures so callers can back off and retry instead of recording a miss.
    """
    import requests
    from urllib.parse import urlparse
    from ..http import _s2_headers, _throttle
    host = urlparse(url).hostname or ""
    _throttle(host)
    try:
        headers = {"User-Agent": "paperlab_publish/0.2"}
        headers.update(_s2_headers())
        r = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise TransientError(str(exc))
    if r.status_code in (429, 500, 502, 503, 504):
        raise TransientError(f"HTTP {r.status_code}")
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        # 4xx other than 404 means a genuinely missing/forbidden record.
        return None
    try:
        return json.loads(r.text)
    except json.JSONDecodeError:
        return None


def _post_strict(url: str, body: dict) -> list | None:
    """POST JSON to S2, returning the parsed response list.

    Raises :class:`TransientError` on 429/5xx/network failures so callers
    can retry the batch. Returns ``None`` for non-transient 4xx errors.
    """
    import requests
    from urllib.parse import urlparse
    from ..http import _s2_headers, _throttle
    host = urlparse(url).hostname or ""
    _throttle(host)
    headers = {"User-Agent": "paperlab_publish/0.2", "Content-Type": "application/json"}
    headers.update(_s2_headers())
    try:
        r = requests.post(url, headers=headers, json=body, timeout=60)
    except requests.RequestException as exc:
        raise TransientError(str(exc))
    if r.status_code in (429, 500, 502, 503, 504):
        raise TransientError(f"HTTP {r.status_code}")
    if r.status_code != 200:
        return None
    try:
        return json.loads(r.text)
    except json.JSONDecodeError:
        return None


def get_batch_strict(ids: list[str]) -> dict[str, dict | None]:
    """Fetch up to 500 papers in one ``POST /paper/batch`` request.

    Each entry in *ids* should use an S2-recognised prefix (``DOI:...``,
    ``ArXiv:...``, or a raw ``paperId`` hash). Returns a dict mapping each
    input id to :func:`normalize` output, or ``None`` when S2 has no record.
    Raises :class:`TransientError` on 429/5xx so the caller can back off.
    """
    if not ids:
        return {}
    url = f"{BASE}/batch?fields={FIELDS}"
    data = _post_strict(url, {"ids": ids})
    if not data or not isinstance(data, list):
        return {id_: None for id_ in ids}
    return {ids[i]: normalize(p) if p else None for i, p in enumerate(data)}


def _authors(p: dict) -> str | None:
    names = [a.get("name") for a in (p.get("authors") or []) if a.get("name")]
    return ", ".join(names) if names else None


def normalize(p: dict) -> dict:
    ext = p.get("externalIds") or {}
    oa = p.get("openAccessPdf") or {}
    return {
        "title": p.get("title"),
        "abstract": p.get("abstract"),
        "year": p.get("year"),
        "arxiv_id": ext.get("ArXiv"),
        "doi": ext.get("DOI"),
        "citation_count": p.get("citationCount"),
        "pdf_url": oa.get("url"),
        "authors": _authors(p),
    }


def match_by_title(title: str) -> dict | None:
    """Best-match paper for a title via S2 search/match."""
    url = f"{BASE}/search/match?query={quote(title)}&fields={FIELDS}"
    d = _get(url)
    if not d:
        return None
    data = d.get("data") or []
    return normalize(data[0]) if data else None


def match_by_title_strict(title: str) -> dict | None:
    """Like ``match_by_title`` but raises :class:`TransientError` on 429/5xx.

    Returns ``None`` only when S2 confirms there is no match, so callers can
    distinguish a permanent miss from a rate-limited request.
    """
    url = f"{BASE}/search/match?query={quote(title)}&fields={FIELDS}"
    d = _get_strict(url)
    if not d:
        return None
    data = d.get("data") or []
    return normalize(data[0]) if data else None


def get_by_doi_strict(doi: str) -> dict | None:
    """Like ``get_by_doi`` but raises :class:`TransientError` on 429/5xx."""
    cleaned = (doi or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    if not cleaned:
        return None
    url = f"{BASE}/DOI:{cleaned}?fields={FIELDS}"
    d = _get_strict(url)
    return normalize(d) if d else None


def get_by_arxiv(arxiv_id: str) -> dict | None:
    url = f"{BASE}/ArXiv:{arxiv_id}?fields={FIELDS}"
    d = _get(url)
    return normalize(d) if d else None


def get_by_doi(doi: str) -> dict | None:
    """Fetch a Semantic Scholar paper by its (normalized) DOI."""
    doi = (doi or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
    if not doi:
        return None
    url = f"{BASE}/DOI:{doi}?fields={FIELDS}"
    d = _get(url)
    return normalize(d) if d else None
