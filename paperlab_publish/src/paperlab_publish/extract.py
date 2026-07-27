"""LLM structured extraction: target_problem / core_idea / organizations."""
from __future__ import annotations

from . import pdf as pdfmod
from .llm import chat_json

_SYS = (
    "You extract structured metadata from computer-science research papers. "
    "Given the title, authors, abstract, and the first pages of text, return JSON with keys: "
    '"target_problem" (the problem the paper aims to solve, 1-3 sentences), '
    '"core_idea" (the central approach or insight, 1-3 sentences), '
    '"organizations" (comma-separated author affiliations lifted from the first pages; '
    "use an empty string if genuinely unknown). Be concise and factual. Do not invent."
)


def extract_paper(title, authors, abstract, pdf_path, *, model: str) -> dict:
    head = pdfmod.extract_text_pages(pdf_path, 2)[:6000]
    user = (
        f"Title: {title}\n"
        f"Authors: {authors or ''}\n"
        f"Abstract: {abstract or ''}\n\n"
        f"First pages:\n{head}"
    )
    return chat_json(_SYS, user, model=model)
