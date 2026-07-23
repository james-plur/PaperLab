"""Framework-figure extraction: crop from PDF when possible, else LLM Mermaid.

PDF heuristic: scan the first ~12 pages for a caption matching an
architecture/overview keyword, then render the band of the page immediately
above that caption to PNG (captures both raster and vector figures). If no such
caption is found, fall back to an LLM-generated Mermaid graph built from the
extracted target_problem + core_idea.
"""
from __future__ import annotations

import re
from pathlib import Path

from .llm import chat_json

CAPTION_RE = re.compile(r"(Figure|Fig\.?)\s*\d+\s*[:.\-\u2014]?\s*(.+)", re.I)
ARCH_RE = re.compile(
    r"architect|overview|framework|pipeline|system design|methodolog|"
    r"workflow|data flow|block diagram|system overview|design", re.I)


def detect_from_pdf(pdf_path, dest_png) -> bool:
    """Try to crop an architecture/overview figure; return True on success."""
    import pymupdf

    doc = pymupdf.open(pdf_path)
    best = None  # (page_index, clip_rect)
    for pi in range(min(doc.page_count, 12)):
        page = doc[pi]
        blocks = page.get_text("blocks")
        text_blocks = [b for b in blocks if b[6] == 0]
        for b in text_blocks:
            txt = b[4]
            cm = CAPTION_RE.search(txt)
            if cm and ARCH_RE.search(txt):
                cap_y0 = b[1]
                above = [tb for tb in text_blocks if tb[3] <= cap_y0 + 2 and tb is not b]
                top = max((tb[3] for tb in above), default=0)
                rect = pymupdf.Rect(
                    max(0, b[0] - 20), max(0, top),
                    min(page.rect.width, b[2] + 60), cap_y0,
                )
                if rect.height > 40:
                    best = (pi, rect)
                    break
        if best:
            break
    if not best:
        doc.close()
        return False
    pi, rect = best
    pix = doc[pi].get_pixmap(clip=rect, dpi=150)
    dest_png = Path(dest_png)
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    pix.save(dest_png)
    doc.close()
    return True


_MERMAID_SYS = (
    "You generate a Mermaid 'graph TD' diagram illustrating the core idea of a research paper. "
    "Return JSON: {\"mermaid\": \"graph TD\\n  A[...] --> B[...]\"}. "
    "Use 4-8 nodes with clear labels capturing the main components and the data/control flow. "
    "Output valid Mermaid only inside the string."
)


def generate_mermaid(title, target_problem, core_idea, *, model: str) -> str:
    user = f"Title: {title}\nProblem: {target_problem}\nCore idea: {core_idea}"
    data = chat_json(_MERMAID_SYS, user, model=model)
    m = (data.get("mermaid") or "").strip()
    return m
