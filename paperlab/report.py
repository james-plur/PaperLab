"""LLM deep-read report: background, problem, solution, evaluation.

The generated Markdown report is written to disk under ``reports/`` so that
users can edit it freely; the database only stores the path and status.
"""
from __future__ import annotations

from pathlib import Path

from . import pdf as pdf_mod
from . import figure as figure_mod
from .llm import chat_text, get_model

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


def generate_report(store, cfg, paper_id: str, *, log=print) -> str:
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
    markdown = chat_text(_SYS, user, model=get_model())

    dest = report_path_for(cfg, paper_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(markdown, encoding="utf-8")
    store.set_report(paper_id, report_path=str(dest), status="ok")
    log(f"  [report] {paper_id}: written -> {dest}")
    return str(dest)
