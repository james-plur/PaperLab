"""Efficient OpenAlex metadata enrichment for papers that already have DOIs."""
from __future__ import annotations

import json
from urllib.parse import urlencode

from ..http import fetch_text


def normalize_doi(value: str | None) -> str:
    doi = str(value or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
    return doi


def reconstruct_abstract(inverted: dict | None) -> str:
    positions = []
    for word, indexes in (inverted or {}).items():
        for index in indexes:
            positions.append((int(index), word))
    return " ".join(word for _, word in sorted(positions))


def _parse_work(work: dict) -> dict:
    authors: list[str] = []
    organizations: list[str] = []
    for authorship in work.get("authorships") or []:
        name = (authorship.get("author") or {}).get("display_name")
        if name and name not in authors:
            authors.append(name)
        for institution in authorship.get("institutions") or []:
            institution_name = institution.get("display_name")
            if institution_name and institution_name not in organizations:
                organizations.append(institution_name)
    location = work.get("primary_location") or {}
    open_access = work.get("open_access") or {}
    abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
    return {
        "source_id": str(work.get("id") or "").rsplit("/", 1)[-1],
        "doi": normalize_doi(work.get("doi")),
        "title": work.get("title") or "",
        "authors": "; ".join(authors),
        "organizations": "; ".join(organizations),
        "abstract": abstract,
        "citation_count": work.get("cited_by_count") or 0,
        "published_date": work.get("publication_date"),
        "source_url": location.get("landing_page_url") or work.get("id") or "",
        "pdf_url": location.get("pdf_url") or open_access.get("oa_url") or "",
        "metadata_status": "complete" if abstract else "partial",
    }


def fetch_dois(dois: list[str]) -> dict[str, dict]:
    """Return OpenAlex works keyed by normalized DOI (up to 50 per request)."""
    requested = [normalize_doi(value) for value in dois if normalize_doi(value)]
    if not requested:
        return {}
    params = urlencode({
        "filter": "doi:" + "|".join(requested),
        "per-page": min(200, len(requested)),
        "select": (
            "id,doi,title,authorships,abstract_inverted_index,cited_by_count,"
            "publication_date,primary_location,open_access"
        ),
        "mailto": "paperlab@localhost",
    })
    raw = fetch_text("https://api.openalex.org/works?" + params,
                     compressed=True, max_time=40, retries=2)
    data = json.loads(raw)
    output: dict[str, dict] = {}
    for work in data.get("results") or []:
        parsed = _parse_work(work)
        if parsed["doi"]:
            output[parsed["doi"]] = parsed
    return output
