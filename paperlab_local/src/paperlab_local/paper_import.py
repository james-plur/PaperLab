"""Validated PDF preview and confirmed manual paper import."""
from __future__ import annotations

import html
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import time
from urllib.parse import urljoin, urlparse
from pathlib import Path

import requests

from .store import Store, title_hash

MAX_PDF_BYTES = 50 * 1024 * 1024
TOKEN_TTL_SECONDS = 30 * 60
FIELDS = ("title", "authors", "organizations", "abstract", "keywords", "doi",
          "arxiv_id", "venue", "year", "source_url", "pdf_url")
LIMITS = {"title": 1000, "authors": 4000, "organizations": 4000,
          "abstract": 30000, "keywords": 2000, "doi": 500, "arxiv_id": 100,
          "venue": 100, "source_url": 2000, "pdf_url": 2000}


class ImportError(ValueError):
    pass


def _public_http_url(value: str) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ImportError("PDF URL must be a public HTTP or HTTPS URL")
    try:
        default_port = 443 if parsed.scheme == "https" else 80
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or default_port)}
    except OSError as exc:
        raise ImportError("PDF URL host could not be resolved") from exc
    import ipaddress
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ImportError("PDF URL must not point to a private or local address")
    return url


def _download_pdf_url(db_path, url: str) -> tuple[str, Path, list[str]]:
    root = Path(db_path).resolve().parent / "import-temp"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    cleanup_previews(db_path)
    token = secrets.token_urlsafe(24)
    path = root / (token + ".pdf")
    current = _public_http_url(url)
    warnings = []
    try:
        for _ in range(6):
            response = requests.get(current, headers={"User-Agent": "PaperLab/0.3"}, timeout=45,
                                    stream=True, allow_redirects=False)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise ImportError("PDF URL redirect has no destination")
                current = _public_http_url(urljoin(current, location))
                continue
            if response.status_code != 200:
                response.close()
                raise ImportError(f"PDF download failed with HTTP {response.status_code}")
            size = 0
            with path.open("wb") as handle:
                for chunk in response.iter_content(1 << 16):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_PDF_BYTES:
                        raise ImportError("PDF exceeds 50 MB limit")
                    handle.write(chunk)
            response.close()
            if size < 8 or path.read_bytes()[:5] != b"%PDF-":
                raise ImportError("downloaded file is not a valid PDF")
            os.chmod(path, 0o600)
            content_type = response.headers.get("Content-Type", "")
            if content_type and "pdf" not in content_type.lower():
                warnings.append("Server returned a non-PDF Content-Type; file signature was valid.")
            return token, path, warnings
        raise ImportError("PDF URL redirected too many times")
    except requests.RequestException as exc:
        raise ImportError("PDF download failed: " + str(exc)[:300]) from exc
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _preview_path(db_path, token, path, *, provider=None, model=None, warnings=None, pdf_url=None):
    local, text = _heuristic_fields(path)
    extracted = dict(local)
    sources = {key: "local" for key, value in local.items() if value not in (None, "")}
    output_warnings = list(warnings or ())
    try:
        llm = _llm_fields(local, text, db_path=db_path, provider=provider, model=model)
        for key, value in llm.items():
            if value not in (None, ""):
                extracted[key] = value
                sources[key] = "llm"
    except Exception as exc:
        output_warnings.append("LLM extraction unavailable; review and complete fields manually: " + str(exc)[:300])
    if pdf_url:
        extracted["pdf_url"] = pdf_url
        sources["pdf_url"] = "url"
    return {"import_token": token, "fields": extracted, "sources": sources,
            "warnings": output_warnings, "duplicate": find_duplicate(db_path, extracted)}


def _clean(value, field):
    if value is None:
        return None
    if field == "year":
        try:
            year = int(value)
        except (TypeError, ValueError):
            return None
        return year if 1900 <= year <= 2100 else None
    text = html.unescape(str(value)).replace("\x00", " ")
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:LIMITS[field]] or None


def clean_fields(values):
    return {field: _clean(values.get(field), field) for field in FIELDS}


def _heuristic_fields(path):
    import pymupdf
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        raise ImportError("invalid or unreadable PDF") from exc
    try:
        if doc.page_count < 1:
            raise ImportError("PDF has no pages")
        metadata = doc.metadata or {}
        page = doc[0]
        blocks = page.get_text("dict").get("blocks", [])
        spans = [span for block in blocks for line in block.get("lines", [])
                 for span in line.get("spans", []) if span.get("text", "").strip()]
        title = _clean(metadata.get("title"), "title")
        if not title or title.lower() in {"untitled", "unknown"}:
            candidates = sorted(spans, key=lambda span: (-float(span.get("size", 0)), span["bbox"][1]))
            title = _clean(" ".join(span["text"] for span in candidates[:3]), "title")
        authors = _clean(metadata.get("author"), "authors")
        text = "\n".join(doc[index].get_text() for index in range(min(2, doc.page_count)))
        match = re.search(r"(?is)\babstract\s*[-—:]?\s*(.{80,6000}?)(?=\n\s*(?:1\.?\s+)?introduction\b)", text)
        abstract = _clean(match.group(1), "abstract") if match else None
        return clean_fields({"title": title, "authors": authors, "abstract": abstract}), text[:14000]
    finally:
        doc.close()


def _llm_fields(local_fields, text, *, db_path, provider=None, model=None):
    from .llm import chat_json
    system = (
        "Extract bibliographic metadata from a computer-science paper. Return one JSON object with "
        "title, authors, organizations, abstract, keywords, doi, arxiv_id, venue, and year. "
        "authors, organizations, and keywords are comma-separated strings. Use null when unknown. "
        "Never return HTML, file paths, or invented values."
    )
    user = "Local candidates:\n" + json.dumps(local_fields, ensure_ascii=False) + "\n\nPDF text:\n" + text
    return clean_fields(chat_json(system, user, provider=provider, model=model, db_path=db_path))


def cleanup_previews(db_path):
    root = Path(db_path).resolve().parent / "import-temp"
    cutoff = time.time() - TOKEN_TTL_SECONDS
    if root.is_dir():
        for path in root.iterdir():
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)


def preview_pdf(db_path, payload, *, provider=None, model=None):
    if len(payload) > MAX_PDF_BYTES:
        raise ImportError("PDF exceeds 50 MB limit")
    if len(payload) < 8 or not payload.startswith(b"%PDF-"):
        raise ImportError("file is not a valid PDF")
    root = Path(db_path).resolve().parent / "import-temp"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    cleanup_previews(db_path)
    token = secrets.token_urlsafe(24)
    path = root / (token + ".pdf")
    path.write_bytes(payload)
    os.chmod(path, 0o600)
    warnings = []
    try:
        return _preview_path(db_path, token, path, provider=provider, model=model, warnings=warnings)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def preview_pdf_url(db_path, url, *, provider=None, model=None):
    token, path, warnings = _download_pdf_url(db_path, url)
    try:
        return _preview_path(db_path, token, path, provider=provider, model=model,
                             warnings=warnings, pdf_url=str(url).strip())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _bibtex_value(entry, *names):
    for name in names:
        value = entry.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return None


def parse_bibtex(text: str) -> list[dict]:
    if not str(text or "").strip():
        raise ImportError("BibTeX is empty")
    try:
        import bibtexparser
        database = bibtexparser.loads(text)
    except Exception as exc:
        raise ImportError("invalid BibTeX: " + str(exc)[:300]) from exc
    if not database.entries:
        raise ImportError("BibTeX contains no entries")
    results = []
    for entry in database.entries:
        title = _bibtex_value(entry, "title")
        if not title:
            continue
        title = re.sub(r"[{}]", "", title)
        authors = _bibtex_value(entry, "author")
        if authors:
            authors = ", ".join(part.strip() for part in re.split(r"\s+and\s+", authors) if part.strip())
        doi = _bibtex_value(entry, "doi")
        url = _bibtex_value(entry, "url")
        eprint = _bibtex_value(entry, "eprint")
        archive = (_bibtex_value(entry, "archiveprefix") or "").lower()
        arxiv_id = eprint if archive == "arxiv" or (eprint and re.search(r"\d{4}\.\d{4,5}", eprint)) else None
        pdf_url = _bibtex_value(entry, "pdf", "pdf_url", "file")
        if pdf_url and not pdf_url.lower().startswith(("http://", "https://")):
            pdf_url = None
        results.append(clean_fields({
            "title": title, "authors": authors,
            "organizations": _bibtex_value(entry, "institution", "affiliation"),
            "abstract": _bibtex_value(entry, "abstract"),
            "keywords": _bibtex_value(entry, "keywords"), "doi": doi,
            "arxiv_id": arxiv_id,
            "venue": _bibtex_value(entry, "booktitle", "journal"),
            "year": _bibtex_value(entry, "year"), "source_url": url,
            "pdf_url": pdf_url,
        }))
    if not results:
        raise ImportError("BibTeX contains no entry with a title")
    return results


def preview_bibtex(db_path, text, *, cfg=None):
    records = []
    for fields in parse_bibtex(text):
        warnings = []
        if cfg:
            try:
                from .related import enrich_candidate
                fields = enrich_candidate(fields, cfg)
            except Exception as exc:
                warnings.append("Metadata enrichment unavailable: " + str(exc)[:200])
        token = None
        if fields.get("pdf_url"):
            try:
                token, _, download_warnings = _download_pdf_url(db_path, fields["pdf_url"])
                warnings.extend(download_warnings)
            except ImportError as exc:
                warnings.append("PDF was not downloaded: " + str(exc))
        records.append({"fields": fields, "import_token": token,
                        "duplicate": find_duplicate(db_path, fields), "warnings": warnings,
                        "has_pdf": bool(token)})
    return {"records": records}


def find_duplicate(db_path, fields):
    values = clean_fields(fields)
    with Store(str(db_path), backup_on_migrate=False) as store:
        row = store._find_existing(values.get("arxiv_id"), values.get("doi"),
                                   title_hash(values.get("title") or "")) if values.get("title") else None
        if not row:
            return None
        return {key: row[key] for key in ("id", "title", "authors", "organizations", "abstract",
                                           "keywords", "doi", "arxiv_id", "venue", "year", "version")}


def finalize_import(db_path, pdf_root, payload):
    cleanup_previews(db_path)
    raw_fields = payload.get("fields") or {}
    fields = clean_fields(raw_fields)
    if not fields.get("title"):
        raise ImportError("title is required")
    duplicate = find_duplicate(db_path, fields)
    action = payload.get("duplicate_action")
    if duplicate and action not in {"fill_missing", "overwrite_fields"}:
        return {"conflict": duplicate}
    token = str(payload.get("import_token") or "")
    temp = None
    if token:
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,80}", token):
            raise ImportError("invalid import token")
        temp = Path(db_path).resolve().parent / "import-temp" / (token + ".pdf")
        if not temp.is_file() or time.time() - temp.stat().st_mtime > TOKEN_TTL_SECONDS:
            raise ImportError("import token expired")
    with Store(str(db_path), backup_on_migrate=False) as store:
        paper_id = store.upsert_candidate({
            **fields,
            "source": raw_fields.get("source") or "manual",
            "source_id": raw_fields.get("source_id") or raw_fields.get("openalex_id") or "",
        })
        current = store.get(paper_id)
        existing_pdf_path = str(current["pdf_path"] or "")
        incoming_citations = payload.get("citation_count")
        if incoming_citations is not None and int(incoming_citations or 0) > int(current["citation_count"] or 0):
            store.set_enrichment(paper_id, {"citation_count": int(incoming_citations)}, commit=True)
    overwritten = []
    if duplicate and action == "overwrite_fields":
        allowed = set(payload.get("overwrite_fields") or ()) & set(FIELDS)
        updates = {key: fields[key] for key in allowed if fields.get(key) not in (None, "")}
        if updates:
            from .web import update_paper
            update_paper(db_path, paper_id, updates)
            overwritten = sorted(updates)
    pdf_path = None
    pdf_conflict = False
    if temp:
        destination = Path(pdf_root).resolve() / "pdfs" / (paper_id.replace(":", "-").replace("/", "_") + ".pdf")
        destination.parent.mkdir(parents=True, exist_ok=True)
        existing_pdf = Path(existing_pdf_path) if existing_pdf_path else destination
        if existing_pdf.is_file() and hashlib.sha256(existing_pdf.read_bytes()).digest() != hashlib.sha256(temp.read_bytes()).digest():
            pdf_conflict = True
            temp.unlink(missing_ok=True)
        elif existing_pdf.is_file():
            temp.unlink(missing_ok=True)
            pdf_path = str(existing_pdf)
        else:
            staged = destination.with_suffix(".pdf.importing")
            shutil.copyfile(temp, staged)
            os.replace(staged, destination)
            temp.unlink(missing_ok=True)
            with Store(str(db_path), backup_on_migrate=False) as store:
                store.set_pdf(paper_id, path=str(destination), status="ok")
            pdf_path = str(destination)
    cleanup_previews(db_path)
    return {"paper_id": paper_id, "created": duplicate is None, "merged": duplicate is not None,
            "overwritten_fields": overwritten, "has_pdf": bool(pdf_path), "pdf_conflict": pdf_conflict}


def finalize_batch(db_path, pdf_root, items):
    results = {"created": [], "merged": [], "failed": []}
    for index, item in enumerate(items or []):
        try:
            payload = dict(item)
            fields = payload.get("fields") or {}
            if not payload.get("import_token") and fields.get("pdf_url"):
                try:
                    payload["import_token"], _, _ = _download_pdf_url(db_path, fields["pdf_url"])
                except ImportError:
                    pass
            payload["duplicate_action"] = "fill_missing"
            result = finalize_import(db_path, pdf_root, payload)
            bucket = "created" if result["created"] else "merged"
            results[bucket].append({"index": index, **result})
        except Exception as exc:
            results["failed"].append({"index": index, "error": str(exc)[:500]})
    return results
