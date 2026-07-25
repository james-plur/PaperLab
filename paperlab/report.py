"""LLM deep-read report: background, problem, solution, evaluation.

The generated Markdown report is written to disk under ``reports/`` so that
users can edit it freely; the database only stores the path and status.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import pdf as pdf_mod
from . import figure as figure_mod
from .llm import chat_text

_SYS = (
    "You are a senior research scientist writing a thorough deep-read report of an "
    "academic paper in Markdown. The report must be written in Chinese. "
    "The report must start with a top-level heading matching the paper title. "
    "\n\n"
    "Right after the title, include a ## \u603b\u7ed3 (Summary) section with the following structure:\n"
    "1. First, one or two sentences summarizing the paper's background and motivation.\n"
    "2. Then a paragraph titled **As-is**: describe the current state of the problem -- what is the baseline approach, what are its limitations and pain points.\n"
    "3. Then a paragraph titled **To-be**: describe what the paper's method achieves -- the key improvements, quantitative gains, and benefits over the baseline.\n"
    "\n\n"
    "After the summary, cover four sections in this order:\n"
    "## \u80cc\u666f (background and motivation),\n"
    "## \u95ee\u9898 (the specific problem the paper tackles),\n"
    "## \u89e3\u51b3\u65b9\u6848 (the proposed method, architecture or key ideas, explained concretely),\n"
    "## \u6d4b\u8bd5\u7ed3\u679c (experimental setup, baselines, main results and limitations).\n"
    "\n\n"
    "Use the title, abstract, and the full extracted PDF text as evidence. Be detailed "
    "and specific -- cite concrete numbers, module names, and design choices from the "
    "paper. Do not fabricate content not supported by the provided text. Output pure "
    "Markdown."
    "\n\n"
    "IMPORTANT: If figure information is provided, insert the figure images at the most "
    "relevant positions in the report using the exact Markdown image syntax provided to "
    "you. Place each figure where it is most relevant to the surrounding discussion."
)


def _slug(paper_id: str) -> str:
    return paper_id.replace("/", "_").replace(":", "-")


def report_path_for(cfg, paper_id: str) -> Path:
    return (cfg.pdf_root / "reports" / f"{_slug(paper_id)}.md").resolve()


def _figures_dir_for(cfg, paper_id: str) -> Path:
    return (cfg.pdf_root / "reports" / "figures" / _slug(paper_id)).resolve()


def generate_report(store, cfg, paper_id: str, *, provider=None, model=None, log=print) -> str:
    """Generate (or regenerate) the deep-read report for one paper.

    Returns the absolute path of the report Markdown file.  Requires that the
    PDF has already been downloaded; raises ``RuntimeError`` otherwise.
    """
    row = store.get(paper_id)
    if row is None:
        raise RuntimeError(f"paper not found: {paper_id}")
    pdf_path_str = row["pdf_path"]
    if not pdf_path_str:
        raise RuntimeError(
            f"paper '{paper_id}' has no downloaded PDF; run `paperlab download` first")
    pdf_path = Path(pdf_path_str)
    if not pdf_path.is_file():
        raise RuntimeError(f"PDF file missing on disk: {pdf_path}")

    full_text = ""
    try:
        full_text = pdf_mod.extract_full_text(pdf_path)
    except Exception as exc:
        log(f"  [report] PDF text extraction failed for {paper_id}: {exc}")
        full_text = ""
    # Cap to keep the prompt manageable; rely on abstract for the rest.
    if len(full_text) > 50000:
        full_text = full_text[:50000]

    # Extract figures from the PDF
    figures_dir = _figures_dir_for(cfg, paper_id)
    figures: list[dict] = []
    try:
        figures = figure_mod.extract_all_figures(pdf_path, figures_dir, max_figures=15)
        log(f"  [report] extracted {len(figures)} figures -> {figures_dir}")
    except Exception as exc:
        log(f"  [report] figure extraction failed for {paper_id}: {exc}")

    figure_info = ""
    if figures:
        lines = ["\n\nAvailable figures (insert at the most relevant position in the report):"]
        for fig in figures:
            # Relative path so the markdown works from the report file location
            rel_path = f"figures/{_slug(paper_id)}/{fig['path']}"
            md_img = f"![Figure {fig['num']}]({rel_path})"
            lines.append(f"- Figure {fig['num']}: {fig['caption'][:150]}")
            lines.append(f"  Use this exact Markdown to insert: {md_img}")
        figure_info = "\n".join(lines)

    user = (
        f"Title: {row['title']}\n"
        f"Authors: {row['authors'] or ''}\n"
        f"Venue: {row['venue'] or ''} {row['year'] or ''}\n"
        f"Abstract: {row['abstract'] or ''}\n\n"
        f"Full paper text:\n{full_text}"
        f"{figure_info}"
    )
    markdown = chat_text(
        _SYS, user, provider=provider, model=model, db_path=cfg.db_path)

    dest = report_path_for(cfg, paper_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(markdown, encoding="utf-8")
    store.set_report(paper_id, report_path=str(dest), status="ok")
    log(f"  [report] {paper_id}: written -> {dest}")
    return str(dest)


_TOPIC_REVIEW_SYS = """You are a senior research scientist writing a Chinese literature review.
Use only the supplied paper metadata and deep-read reports. Do not invent facts.
Start with a top-level heading containing the topic name. Then write:
## 核心问题: identify and explain the central research problems shared by these papers.
## 发展脉络: organize the evolution around those core problems, chronologically. Every
development node must be one supplied paper and state its title, year, problem addressed,
core method, advance over earlier work, and limitations. Explicitly connect adjacent nodes.
## 综合观察: summarize converging ideas, disagreements, gaps, and promising directions.
Every supplied paper must appear as a development node. Output pure Markdown."""


def topic_snapshot_hash(paper_ids: list[str]) -> str:
    return hashlib.sha256("\n".join(paper_ids).encode("utf-8")).hexdigest()


def topic_review_path_for(cfg, topic_id: int) -> Path:
    return (cfg.pdf_root / "reports" / "topics" / f"{topic_id}.md").resolve()


def generate_topic_review(store, cfg, topic_id: int, *, provider=None, model=None,
                          log=print) -> str:
    """Generate a persisted review only when every member report already exists."""

    topic = store.get_collection_topic(topic_id)
    if topic is None:
        raise RuntimeError("topic not found")
    papers = list(store.collection_papers(topic_id))
    if not papers:
        raise ValueError("topic has no papers")
    paper_ids = [row["id"] for row in papers]
    snapshot = topic_snapshot_hash(paper_ids)
    total = len(papers)
    report_inputs = []
    missing = []
    for index, paper in enumerate(papers):
        paper_id = paper["id"]
        existing = store.get_report(paper_id)
        report_path = Path(existing["report_path"]) if existing and existing["report_path"] else None
        if not existing or existing["status"] != "ok" or not report_path or not report_path.is_file():
            missing.append(paper["title"])
            continue
        report_inputs.append(
            f"## Paper {index + 1}\nTitle: {paper['title']}\nYear: {paper['year'] or 'unknown'}\n"
            f"Authors: {paper['authors'] or ''}\nVenue: {paper['venue'] or ''}\n\n"
            f"Deep-read report:\n{report_path.read_text(encoding='utf-8')}"
        )
    if missing:
        names = "; ".join(missing[:10])
        if len(missing) > 10:
            names += f"; and {len(missing) - 10} more"
        raise ValueError(f"missing reports for {len(missing)} paper(s): {names}")

    store.set_topic_review(topic_id, status="running", stage="generating review",
                           completed_papers=total, total_papers=total, snapshot_hash=snapshot)
    user = f"Topic: {topic['name']}\n\n" + "\n\n---\n\n".join(report_inputs)
    markdown = chat_text(
        _TOPIC_REVIEW_SYS, user, provider=provider, model=model, db_path=cfg.db_path)
    dest = topic_review_path_for(cfg, topic_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(markdown, encoding="utf-8")

    current_ids = [row["id"] for row in store.collection_papers(topic_id)]
    current_hash = topic_snapshot_hash(current_ids)
    final_status = "ok" if current_hash == snapshot else "stale"
    final_stage = "complete" if final_status == "ok" else "membership changed; regenerate"
    store.set_topic_review(
        topic_id, status=final_status, stage=final_stage, completed_papers=total,
        total_papers=total, review_path=str(dest), snapshot_hash=snapshot, generated=True)
    return str(dest)
