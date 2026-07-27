"""PDF download + pymupdf text/image extraction + github scanning."""
from __future__ import annotations

import re
from pathlib import Path

from .http import fetch_bytes


def download(url: str, dest) -> int:
    return fetch_bytes(url, dest)


def _open(path):
    import pymupdf
    return pymupdf.open(path)


def extract_text_pages(path, n: int = 2) -> str:
    doc = _open(path)
    out = []
    for i in range(min(n, doc.page_count)):
        out.append(doc[i].get_text())
    doc.close()
    return "\n\n".join(out)


def extract_full_text(path) -> str:
    doc = _open(path)
    out = []
    for i in range(doc.page_count):
        out.append(doc[i].get_text())
    doc.close()
    return "\n\n".join(out)


def find_github_url(path, *, title: str | None = None) -> str | None:
    """Return the best owner/repo URL found in the PDF, or None. Never invents.

    Selection order: earliest page first (a project's own "code available at"
    link sits in the abstract on page 0, while dependencies live in the
    references deep in the paper), then a name token shared with the title,
    then frequency, then shortest (project root over deep subpaths).
    """
    import pymupdf
    try:
        doc = pymupdf.open(path)
    except Exception:
        return None
    repos: list[tuple[str, int]] = []  # (owner/repo, page_index)
    try:
        for pi in range(doc.page_count):
            t = doc[pi].get_text()
            for m in re.finditer(r"github\.com/([A-Za-z0-9][\w.-]*?)/([A-Za-z0-9_.-]+)", t):
                owner = m.group(1).rstrip("/.,;)]")
                repo = m.group(2).rstrip(".,;)]")
                if repo.endswith(".git"):
                    repo = repo[:-4]
                if not repo or repo.lower() in {"tree", "blob", "raw", "pulls", "issues",
                                                "releases", "wiki", "settings", "notifications",
                                                "stars", "watchers", "network"}:
                    continue
                repos.append((f"{owner}/{repo}", pi))
    finally:
        doc.close()
    if not repos:
        return None
    from collections import Counter
    counts = Counter(r for r, _ in repos)
    first_page = {r: min(p for r2, p in repos if r2 == r) for r, _ in repos}
    title_tokens = {w.lower() for w in re.findall(r"[A-Za-z0-9]+", title or "") if len(w) >= 3}
    def rank(r):
        owner, _, name = r.partition("/")
        hit = 1 if title_tokens & ({owner.lower(), name.lower()}) else 0
        short = -len(r)  # prefer shorter (project root over deep subpaths)
        # earliest page dominates; then title token, frequency, shortness.
        return (-first_page[r], hit, counts[r], short)
    best = sorted(counts, key=rank, reverse=True)[0]
    return "github.com/" + best
