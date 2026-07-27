"""Validated PDF preview and confirmed manual paper import."""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path

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
        local, text = _heuristic_fields(path)
        extracted = dict(local)
        sources = {key: "local" for key, value in local.items() if value not in (None, "")}
        try:
            llm = _llm_fields(local, text, db_path=db_path, provider=provider, model=model)
            for key, value in llm.items():
                if value not in (None, ""):
                    extracted[key] = value
                    sources[key] = "llm"
        except Exception as exc:
            warnings.append("LLM extraction unavailable; review and complete fields manually: " + str(exc)[:300])
        duplicate = find_duplicate(db_path, extracted)
        return {"import_token": token, "fields": extracted, "sources": sources,
                "warnings": warnings, "duplicate": duplicate}
    except Exception:
        path.unlink(missing_ok=True)
        raise


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
    fields = clean_fields(payload.get("fields") or {})
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
        paper_id = store.upsert_candidate({**fields, "source": "manual"})
    overwritten = []
    if duplicate and action == "overwrite_fields":
        allowed = set(payload.get("overwrite_fields") or ()) & set(FIELDS)
        updates = {key: fields[key] for key in allowed if fields.get(key) not in (None, "")}
        if updates:
            from .web import update_paper
            update_paper(db_path, paper_id, updates)
            overwritten = sorted(updates)
    pdf_path = None
    if temp:
        destination = Path(pdf_root).resolve() / "pdfs" / (paper_id.replace(":", "-").replace("/", "_") + ".pdf")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staged = destination.with_suffix(".pdf.importing")
        shutil.copyfile(temp, staged)
        os.replace(staged, destination)
        temp.unlink(missing_ok=True)
        with Store(str(db_path), backup_on_migrate=False) as store:
            store.set_pdf(paper_id, path=str(destination), status="ok")
        pdf_path = str(destination)
    cleanup_previews(db_path)
    return {"paper_id": paper_id, "created": duplicate is None, "merged": duplicate is not None,
            "overwritten_fields": overwritten, "has_pdf": bool(pdf_path)}
