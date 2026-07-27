"""Versioned LLM relevance scoring against one or more topics."""
from __future__ import annotations

import hashlib
import json

from .llm import chat_json

_SYS = (
    "You are a research relevance judge. Score how relevant each candidate paper is "
    "to the given research topic, from 0.0 (irrelevant) to 1.0 (highly relevant), "
    "based on its title and abstract (abstract may be empty). Return JSON: "
    "{\"scores\":[{\"id\":\"<paper id>\",\"score\":0.0,\"reason\":\"<=8 words\"}]}. "
    "Judge on topical fit, not quality. Include one entry per input paper, in order."
)


def score_batch(rows, topic_name: str, keywords: list[str], *, model: str, batch: int = 50):
    """rows: list of sqlite3.Row with id/title/abstract. Returns [(id, score, reason)]."""
    results = []
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        lines = [f"Topic: {topic_name}", f"Keywords: {', '.join(keywords)}", "Papers:"]
        for r in chunk:
            ab = (r["abstract"] or "").strip()[:600]
            lines.append(f"- id={r['id']} | title={r['title']} | abstract={ab}")
        data = chat_json(_SYS, "\n".join(lines), model=model)
        for item in data.get("scores", []):
            _id = item.get("id")
            if not _id:
                continue
            try:
                results.append((_id, float(item.get("score", 0.0)), str(item.get("reason", ""))[:200]))
            except (TypeError, ValueError):
                continue
    return results


PROMPT_VERSION = 2

_MULTI_SYS = (
    "You classify academic-paper topical relevance. For every paper and every supplied topic, "
    "return a score from 0.0 (unrelated) to 1.0 (directly about the topic), plus a concise reason. "
    "Judge subject fit, not paper quality. Do not infer content absent from title/abstract. "
    "Return JSON exactly as {\"papers\":[{\"id\":\"...\",\"topics\":"
    "{\"topic-id\":{\"score\":0.0,\"reason\":\"...\"}}}]}."
)


def definition_hash(topic) -> str:
    payload = {
        "name": topic.name,
        "description": topic.description,
        "keywords": topic.keywords,
        "threshold": topic.relevance_threshold,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def score_multi_batch(rows, topics, *, model: str) -> dict[str, dict[str, tuple[float, str]]]:
    lines = ["Topics:"]
    for topic in topics:
        detail = topic.description or ", ".join(topic.keywords) or topic.display_name or topic.name
        lines.append(f"- {topic.name}: {detail}")
    lines.append("Papers:")
    for row in rows:
        abstract = " ".join((row["abstract"] or "").split())[:1200]
        lines.append(
            f"- id={row['id']} | venue={row['venue'] or ''} | title={row['title']} | abstract={abstract}"
        )
    data = chat_json(_MULTI_SYS, "\n".join(lines), model=model)
    output: dict[str, dict[str, tuple[float, str]]] = {}
    valid_ids = {row["id"] for row in rows}
    valid_topics = {topic.name for topic in topics}
    for paper in data.get("papers", []):
        paper_id = str(paper.get("id") or "")
        if paper_id not in valid_ids:
            continue
        values: dict[str, tuple[float, str]] = {}
        for topic_id, result in (paper.get("topics") or {}).items():
            if topic_id not in valid_topics or not isinstance(result, dict):
                continue
            try:
                score = max(0.0, min(1.0, float(result.get("score", 0.0))))
            except (TypeError, ValueError):
                continue
            values[topic_id] = (score, str(result.get("reason") or "")[:500])
        output[paper_id] = values
    return output
