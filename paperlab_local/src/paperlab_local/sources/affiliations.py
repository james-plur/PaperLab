"""Best-effort authoritative affiliation enrichment.

paper-search-mcp 0.1.4 standardizes authors but discards source affiliation
objects.  These narrow API lookups fill that one missing field without using an
LLM or guessing institutions.
"""
from __future__ import annotations

from urllib.parse import quote

from ..http import fetch_text
import json


def openalex_affiliations(*, doi: str = "", title: str = "") -> list[str]:
    if doi:
        url = "https://api.openalex.org/works/https://doi.org/" + quote(doi, safe="/")
    elif title:
        url = "https://api.openalex.org/works?search=" + quote(title) + "&per_page=1"
    else:
        return []
    try:
        data = json.loads(fetch_text(url, max_time=30))
    except (RuntimeError, ValueError):
        return []
    if "results" in data:
        results = data.get("results") or []
        data = results[0] if results else {}
    names: list[str] = []
    for authorship in data.get("authorships") or []:
        for institution in authorship.get("institutions") or []:
            name = str(institution.get("display_name") or "").strip()
            if name and name not in names:
                names.append(name)
    return names
