"""Related-paper discovery through OpenAlex with Semantic Scholar fallback."""
from __future__ import annotations

import json
import math
import os
import re
from urllib.parse import quote, urlencode

import requests

from .store import Store, normalize_title

OPENALEX = "https://api.openalex.org"
S2 = "https://api.semanticscholar.org/graph/v1"
WORK_FIELDS = ("id,doi,title,display_name,publication_year,authorships,"
               "primary_location,best_oa_location,open_access,cited_by_count,"
               "referenced_works,abstract_inverted_index,ids")


def _get_json(url: str, *, timeout=40) -> dict:
    headers = {"User-Agent": "PaperLab/0.3 (mailto:paperlab-local@example.invalid)"}
    if "semanticscholar.org" in url and os.environ.get("S2_API_KEY"):
        headers["x-api-key"] = os.environ["S2_API_KEY"]
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    value = response.json()
    return value if isinstance(value, dict) else {}


def _abstract(inverted) -> str | None:
    if not isinstance(inverted, dict):
        return None
    positioned = [(position, word) for word, positions in inverted.items()
                  for position in positions if isinstance(position, int)]
    return " ".join(word for _, word in sorted(positioned)) or None


def _openalex_candidate(work: dict, relation: str | None = None) -> dict:
    authors, organizations = [], []
    for authorship in work.get("authorships") or []:
        name = (authorship.get("author") or {}).get("display_name")
        if name and name not in authors:
            authors.append(name)
        for institution in authorship.get("institutions") or []:
            name = institution.get("display_name")
            if name and name not in organizations:
                organizations.append(name)
    location = work.get("best_oa_location") or work.get("primary_location") or {}
    ids = work.get("ids") or {}
    doi = work.get("doi") or ids.get("doi")
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(doi or ""), flags=re.I) or None
    arxiv = ids.get("arxiv")
    arxiv_match = re.search(r"(\d{4}\.\d{4,5})", str(arxiv or ""))
    source = (work.get("primary_location") or {}).get("source") or {}
    return {
        "source": "openalex", "source_id": str(work.get("id") or "").rsplit("/", 1)[-1],
        "title": work.get("title") or work.get("display_name") or "",
        "authors": ", ".join(authors), "organizations": ", ".join(organizations),
        "abstract": _abstract(work.get("abstract_inverted_index")), "doi": doi,
        "arxiv_id": arxiv_match.group(1) if arxiv_match else None,
        "venue": source.get("display_name"), "year": work.get("publication_year"),
        "citation_count": int(work.get("cited_by_count") or 0),
        "pdf_url": location.get("pdf_url"), "source_url": location.get("landing_page_url") or work.get("doi") or work.get("id"),
        "relation": relation, "openalex_id": str(work.get("id") or "").rsplit("/", 1)[-1],
    }


def _resolve_openalex(fields: dict) -> dict:
    if fields.get("doi"):
        identifier = "https://doi.org/" + str(fields["doi"]).lower().removeprefix("https://doi.org/")
        return _get_json(f"{OPENALEX}/works/{quote(identifier, safe=':/')}?select={WORK_FIELDS}")
    if fields.get("arxiv_id"):
        identifier = "https://arxiv.org/abs/" + fields["arxiv_id"]
        try:
            return _get_json(f"{OPENALEX}/works/{quote(identifier, safe=':/')}?select={WORK_FIELDS}")
        except requests.HTTPError:
            pass
    title = str(fields.get("title") or "").strip()
    if title:
        params = urlencode({"search": title, "per-page": 5, "select": WORK_FIELDS})
        results = _get_json(f"{OPENALEX}/works?{params}").get("results") or []
        exact = [work for work in results if normalize_title(work.get("title") or "") == normalize_title(title)]
        if exact:
            return exact[0]
    raise LookupError("paper could not be resolved in OpenAlex")


def enrich_candidate(fields: dict, cfg=None) -> dict:
    work = _resolve_openalex(fields)
    candidate = _openalex_candidate(work)
    merged = dict(fields)
    for key, value in candidate.items():
        if merged.get(key) in (None, "", 0) and value not in (None, "", 0):
            merged[key] = value
    if int(candidate.get("citation_count") or 0) > int(merged.get("citation_count") or 0):
        merged["citation_count"] = candidate["citation_count"]
    return merged


def _openalex_related(seed: dict, limit: int) -> list[dict]:
    work = _resolve_openalex(seed)
    seed_id = str(work["id"]).rsplit("/", 1)[-1]
    references = [str(value).rsplit("/", 1)[-1] for value in work.get("referenced_works") or []]
    output = []
    for offset in range(0, min(len(references), 300), 50):
        ids = "|".join(references[offset:offset + 50])
        params = urlencode({"filter": "openalex_id:" + ids, "per-page": 50, "select": WORK_FIELDS})
        for item in _get_json(f"{OPENALEX}/works?{params}").get("results") or []:
            output.append(_openalex_candidate(item, "reference"))
    params = urlencode({"filter": "cites:" + seed_id, "sort": "cited_by_count:desc",
                        "per-page": min(100, max(limit * 5, 30)), "select": WORK_FIELDS})
    for item in _get_json(f"{OPENALEX}/works?{params}").get("results") or []:
        output.append(_openalex_candidate(item, "citation"))
    return output


def _s2_identifier(seed: dict) -> str:
    if seed.get("doi"):
        return "DOI:" + seed["doi"]
    if seed.get("arxiv_id"):
        return "ARXIV:" + seed["arxiv_id"]
    raise LookupError("Semantic Scholar fallback requires DOI or arXiv ID")


def _s2_related(seed: dict) -> list[dict]:
    fields = ("title,abstract,year,venue,authors,externalIds,citationCount,openAccessPdf,url,"
              "references.title,references.abstract,references.year,references.venue,references.authors,"
              "references.externalIds,references.citationCount,references.openAccessPdf,references.url,"
              "citations.title,citations.abstract,citations.year,citations.venue,citations.authors,"
              "citations.externalIds,citations.citationCount,citations.openAccessPdf,citations.url")
    data = _get_json(f"{S2}/paper/{quote(_s2_identifier(seed), safe=':')}?fields={fields}")
    output = []
    for relation, key in (("reference", "references"), ("citation", "citations")):
        for paper in data.get(key) or []:
            ids = paper.get("externalIds") or {}
            output.append({
                "source": "semantic_scholar", "source_id": paper.get("paperId") or "",
                "title": paper.get("title") or "", "authors": ", ".join(a.get("name", "") for a in paper.get("authors") or []),
                "abstract": paper.get("abstract"), "doi": ids.get("DOI"), "arxiv_id": ids.get("ArXiv"),
                "venue": paper.get("venue"), "year": paper.get("year"),
                "citation_count": int(paper.get("citationCount") or 0),
                "pdf_url": (paper.get("openAccessPdf") or {}).get("url"), "source_url": paper.get("url"),
                "relation": relation,
            })
    return output


def _company_matches(candidate: dict, aliases: dict[str, list[str]]) -> list[str]:
    haystack = (str(candidate.get("organizations") or "") + " " + str(candidate.get("title") or "")).lower()
    return [company for company, values in aliases.items()
            if any(alias.lower() in haystack for alias in values)]


def find_related(db_path, cfg, paper_id: str) -> dict:
    with Store(str(db_path), backup_on_migrate=False) as store:
        row = store.get(paper_id)
        if not row:
            raise LookupError("paper not found")
        seed = dict(row)
        existing = {normalize_title(item[0]) for item in store.conn.execute("SELECT title FROM papers")}
    source = "openalex"
    try:
        raw = _openalex_related(seed, cfg.related.result_limit)
    except Exception as openalex_error:
        source = "semantic_scholar"
        try:
            raw = _s2_related(seed)
        except Exception as s2_error:
            raise RuntimeError(f"related-paper lookup failed (OpenAlex: {openalex_error}; Semantic Scholar: {s2_error})") from s2_error
    merged = {}
    seed_title = normalize_title(seed["title"])
    for item in raw:
        norm = normalize_title(item.get("title") or "")
        if not norm or norm == seed_title:
            continue
        key = str(item.get("doi") or item.get("arxiv_id") or norm).lower()
        if key in merged:
            relations = set(str(merged[key].get("relation") or "").split(","))
            relations.add(item.get("relation"))
            merged[key]["relation"] = ",".join(sorted(x for x in relations if x))
            continue
        item["companies"] = _company_matches(item, cfg.related.company_aliases)
        item["score"] = round(math.log1p(int(item.get("citation_count") or 0)) + (2 if item["companies"] else 0), 4)
        item["in_library"] = norm in existing
        merged[key] = item
    candidates = sorted(merged.values(), key=lambda item: (
        float(item["score"]), int(item.get("citation_count") or 0), int(item.get("year") or 0)), reverse=True)
    return {"source": source, "candidates": candidates[:cfg.related.result_limit], "total": len(candidates)}
