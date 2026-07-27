"""Metadata normalization and exact-title enrichment via paper-search-mcp."""
from __future__ import annotations

import ast
import re
from difflib import SequenceMatcher
from typing import Any

from ..mcp_client import PaperSearchMCP
from ..store import normalize_title


def _extra(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = ast.literal_eval(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, SyntaxError):
        return {}


def _arxiv_id(paper: dict) -> str | None:
    if paper.get("source") == "arxiv":
        match = re.search(r"\d{4}\.\d{4,5}", str(paper.get("paper_id") or ""))
        if match:
            return match.group(0)
    for value in (paper.get("doi"), paper.get("url"), paper.get("pdf_url")):
        match = re.search(r"(?:arxiv[.:/]|abs/|pdf/)(\d{4}\.\d{4,5})", str(value or ""), re.I)
        if match:
            return match.group(1)
    return None


def normalize_result(paper: dict) -> dict:
    extra = _extra(paper.get("extra"))
    published = str(paper.get("published_date") or "")
    year = extra.get("year")
    if not year and len(published) >= 4 and published[:4].isdigit():
        year = int(published[:4])
    authors = paper.get("authors") or ""
    return {
        "paper_id": paper.get("paper_id") or "",
        "source_id": paper.get("paper_id") or "",
        "source": paper.get("source") or "",
        "title": paper.get("title") or "",
        "authors": authors,
        "abstract": paper.get("abstract") or "",
        "doi": paper.get("doi") or "",
        "arxiv_id": _arxiv_id(paper),
        "published_date": published or None,
        "year": int(year) if str(year).isdigit() else None,
        "venue": extra.get("venue") or None,
        "pdf_url": paper.get("pdf_url") or "",
        "source_url": paper.get("url") or "",
        "citation_count": paper.get("citations") or 0,
        "raw": paper,
    }


def title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_title(left), normalize_title(right)).ratio()


def enrich_title(client: PaperSearchMCP, title: str, *, sources: list[str],
                 year: int | None = None) -> tuple[dict, list[dict]]:
    response = client.search(title, sources=sources,
                             year=str(year) if year else None,
                             max_results_per_source=3)
    matches = []
    for raw in response.get("papers") or []:
        paper = normalize_result(raw)
        similarity = title_similarity(title, paper["title"])
        if similarity >= 0.90:
            paper["similarity"] = similarity
            matches.append(paper)
    matches.sort(key=lambda p: (
        p["similarity"], bool(p.get("abstract")), bool(p.get("pdf_url")),
        int(p.get("citation_count") or 0)), reverse=True)
    merged: dict[str, Any] = {}
    for paper in matches:
        for key in ("title", "authors", "abstract", "doi", "arxiv_id", "published_date",
                    "year", "venue", "pdf_url", "source_url", "citation_count"):
            if merged.get(key) in (None, "", 0) and paper.get(key) not in (None, "", 0):
                merged[key] = paper[key]
    if matches:
        merged["metadata_status"] = "complete" if merged.get("abstract") else "partial"
    return merged, matches
