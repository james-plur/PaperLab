"""Abstract-only Chinese summaries for PaperLab papers."""
from __future__ import annotations

import hashlib


def abstract_hash(value: str | None) -> str:
    return hashlib.sha256((value or "").strip().encode("utf-8")).hexdigest()


def generate_summary(store, paper_id: str) -> dict:
    from . import llm

    paper = store.get(paper_id)
    if paper is None:
        raise KeyError("paper not found")
    abstract = str(paper["abstract"] or "").strip()
    if not abstract:
        raise ValueError("论文没有摘要，无法生成概要")
    system = (
        "你是严谨的科研文献编辑。仅依据提供的论文元数据和摘要，写一段自然、紧凑的中文概要，"
        "说明研究背景、核心问题、主要方法和摘要明确给出的结论。不要使用列表，不要引用 PDF，"
        "不要补造摘要中没有的信息；信息不足时明确说摘要未说明。"
    )
    user = (
        f"标题：{paper['title']}\n作者：{paper['authors'] or ''}\n"
        f"会议/期刊：{paper['venue'] or ''}\n年份：{paper['year'] or ''}\n\n摘要：{abstract}"
    )
    result = llm.chat_text_fallback(system, user, db_path=store.db_path, temperature=0.2)
    row = store.set_summary(
        paper_id, content=result["answer"].strip(), abstract_hash=abstract_hash(abstract),
        provider=result.get("provider"), model=result.get("model"),
    )
    return dict(row)


def public_summary(store, paper_id: str) -> dict:
    paper = store.get(paper_id)
    if paper is None:
        raise KeyError("paper not found")
    row = store.get_summary(paper_id)
    if row is None:
        return {"paper_id": paper_id, "status": "none", "stale": False, "version": 0}
    value = dict(row)
    value["stale"] = value.get("abstract_hash") != abstract_hash(paper["abstract"])
    value.pop("deleted_at", None)
    return value
