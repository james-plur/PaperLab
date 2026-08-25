"""Persistent reading notes and grounded PDF question answering for PaperLab.app."""
from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path


PRIMARY_MODEL = "GLM-4.7-Flash"
FALLBACK_MODEL = "deepseek-v4-flash"
MAX_PDF_CONTEXT = 120_000


def _note_path(pdf_root: Path, paper_id: str) -> Path:
    digest = hashlib.sha256(paper_id.encode("utf-8")).hexdigest()[:24]
    return Path(pdf_root) / "reading-notes" / f"{digest}.md"


def load_note(pdf_root: Path, paper_id: str) -> str:
    path = _note_path(pdf_root, paper_id)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def save_note(pdf_root: Path, paper_id: str, content: str) -> Path:
    path = _note_path(pdf_root, paper_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".md.tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, path)
    return path


def reconcile_notes(store, pdf_root: Path) -> int:
    """Register pre-schema or externally edited Markdown notes for synchronization."""
    changed = 0
    for row in store.conn.execute("SELECT id FROM papers WHERE deleted_at IS NULL"):
        paper_id = row[0]
        path = _note_path(pdf_root, paper_id)
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        current = store.get_note(paper_id)
        if current and current["content_hash"] == digest:
            continue
        if len(content.encode("utf-8")) > 5 * 1024 * 1024:
            if current:
                store.conn.execute("UPDATE paper_notes SET sync_error=? WHERE paper_id=?",
                                   ("note exceeds 5 MB sync limit", paper_id))
                store.conn.commit()
            continue
        store.set_note(paper_id, note_path=str(path), content=content)
        changed += 1
    return changed


@lru_cache(maxsize=16)
def _extract_pdf_text_cached(path: str, mtime_ns: int) -> str:
    import pymupdf
    chunks: list[str] = []
    size = 0
    with pymupdf.open(path) as document:
        for page_number, page in enumerate(document, 1):
            text = page.get_text("text").strip()
            if not text:
                continue
            chunk = f"\n\n--- Page {page_number} ---\n{text}"
            remaining = MAX_PDF_CONTEXT - size
            if remaining <= 0:
                break
            chunks.append(chunk[:remaining])
            size += len(chunks[-1])
    return "".join(chunks).strip()


def extract_pdf_text(pdf_path: str) -> str:
    path = Path(pdf_path).resolve()
    stat = path.stat()
    return _extract_pdf_text_cached(str(path), stat.st_mtime_ns)


def ask_pdf(db_path: Path, paper: dict, question: str, history: list[dict] | None = None) -> dict:
    from . import llm

    question = question.strip()
    if not question:
        raise ValueError("question is required")
    pdf_path = str(paper.get("pdf_path") or "")
    if not pdf_path or not Path(pdf_path).is_file():
        raise ValueError("paper PDF is not available")
    context = extract_pdf_text(pdf_path)
    if not context:
        raise ValueError("no readable text was found in this PDF")
    recent = []
    for item in (history or [])[-6:]:
        role = "User" if item.get("role") == "user" else "Assistant"
        recent.append(f"{role}: {str(item.get('content') or '')[:4000]}")
    system = (
        "You are PaperLab's research reading assistant. Answer strictly from the supplied "
        "paper text. Be precise, distinguish facts from inference, cite page numbers using "
        "[p.N] when the page marker supports the statement, and say when the PDF does not "
        "contain enough evidence. Reply in the language used by the user."
    )
    user = (
        f"Paper title: {paper.get('title', '')}\n"
        f"Authors: {paper.get('authors', '')}\n\n"
        f"Recent conversation:\n{chr(10).join(recent) or '(none)'}\n\n"
        f"Question: {question}\n\nPDF text:\n{context}"
    )
    return llm.chat_text_fallback(system, user, db_path=db_path, temperature=0.2)
