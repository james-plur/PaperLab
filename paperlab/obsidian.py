"""Render the SQLite catalogue as Obsidian Markdown notes and Bases."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import yaml

from .config import Config, TopicConfig
from .store import Store


def _slug(paper_id: str) -> str:
    return paper_id.replace(":", "-").replace("/", "_")


def _list_value(row, json_key: str, fallback_key: str) -> list[str]:
    try:
        value = json.loads(row[json_key] or "[]")
        if isinstance(value, list):
            return [str(x) for x in value if str(x).strip()]
    except (ValueError, TypeError):
        pass
    fallback = row[fallback_key] or ""
    return [x.strip() for x in str(fallback).split(";") if x.strip()]


def _score_key(topic_id: str) -> str:
    return "score_" + topic_id.replace("-", "_")


def _frontmatter(store: Store, row, topics: dict[str, TopicConfig],
                 scores=None) -> tuple[dict, list]:
    scores = store.topic_scores(row["id"]) if scores is None else scores
    tags = ["paper"]
    properties = {
        "paper_id": row["id"],
        "title": row["title"],
        "authors": _list_value(row, "authors_json", "authors"),
        "organizations": _list_value(row, "organizations_json", "organizations"),
        "venue": row["venue"] or "",
        "year": row["year"],
        "doi": row["doi"] or "",
        "arxiv_id": row["arxiv_id"] or "",
        "source_url": row["source_url"] or "",
        "pdf_url": row["pdf_url"] or "",
        "pdf_path": row["pdf_path"] or "",
        "pdf_status": row["pdf_status"] or "not-downloaded",
        "metadata_status": row["metadata_status"] or "pending",
        "updated_at": int(row["last_updated"] or 0),
    }
    for score in scores:
        properties[_score_key(score["topic_id"])] = round(float(score["score"]), 4)
        if score["relevant"]:
            tags.append("paper/topic/" + score["topic_id"])
    properties["tags"] = tags
    return properties, scores


def render_note(store: Store, row, topics: dict[str, TopicConfig], scores=None) -> str:
    properties, scores = _frontmatter(store, row, topics, scores=scores)
    frontmatter = yaml.safe_dump(properties, allow_unicode=True, sort_keys=False).strip()
    lines = ["---", frontmatter, "---", "", f"# {row['title']}", ""]
    if row["abstract"]:
        lines.extend(["## Abstract", "", str(row["abstract"]).strip(), ""])
    lines.extend(["## Topic relevance", "", "| Topic | Score | Relevant | Reason |",
                  "|---|---:|:---:|---|"])
    for score in scores:
        topic = topics.get(score["topic_id"])
        display = topic.display_name if topic else score["topic_id"]
        lines.append(
            f"| {display} | {float(score['score']):.3f} | "
            f"{'yes' if score['relevant'] else 'no'} | {score['reason'] or ''} |"
        )
    if not scores:
        lines.append("| — | — | — | Not scored |")
    lines.extend(["", "## Links", ""])
    if row["source_url"]:
        lines.append(f"- [Paper page]({row['source_url']})")
    if row["pdf_url"]:
        lines.append(f"- [Remote PDF]({row['pdf_url']})")
    if row["pdf_path"]:
        url = "file://" + quote(str(Path(row["pdf_path"]).resolve()), safe="/")
        lines.append(f"- [Local PDF]({url})")
    if row["doi"]:
        lines.append(f"- [DOI](https://doi.org/{row['doi']})")
    if row["arxiv_id"]:
        lines.append(f"- [arXiv](https://arxiv.org/abs/{row['arxiv_id']})")
    return "\n".join(lines).rstrip() + "\n"


def _base_data(folder: str, topics: list[TopicConfig]) -> dict:
    paper_folder = f"{folder}/Papers"
    properties: dict = {
        "title": {"displayName": "Title"},
        "venue": {"displayName": "Venue"},
        "year": {"displayName": "Year"},
        "authors": {"displayName": "Authors"},
        "organizations": {"displayName": "Organizations"},
        "pdf_status": {"displayName": "PDF"},
        "metadata_status": {"displayName": "Metadata"},
    }
    base_order = ["title", "venue", "year", "authors", "organizations", "pdf_status"]
    views = [{"type": "table", "name": "All Papers", "order": base_order}]
    for topic in topics:
        key = _score_key(topic.name)
        properties[key] = {"displayName": topic.display_name + " score"}
        views.append({
            "type": "table", "name": topic.display_name,
            "filters": f'file.hasTag("paper/topic/{topic.name}")',
            "order": ["title", key, "venue", "year", "authors", "pdf_status"],
        })
    views.extend([
        {"type": "table", "name": "Downloaded",
         "filters": 'pdf_status == "ok"', "order": base_order},
        {"type": "table", "name": "Missing Metadata",
         "filters": 'metadata_status != "complete"',
         "order": ["title", "venue", "year", "metadata_status"]},
    ])
    return {
        "filters": {"and": [f'file.inFolder("{paper_folder}")', 'file.ext == "md"']},
        "properties": properties,
        "views": views,
    }


def sync(store: Store, cfg: Config, *, topic_name: str | None = None, log=print) -> dict:
    root = cfg.obsidian.output_dir
    papers_dir = root / "Papers"
    papers_dir.mkdir(parents=True, exist_ok=True)
    topics = {topic.name: topic for topic in cfg.topics}
    if topic_name:
        paper_ids = {row["id"] for row in store.topic_papers_v2(topic_name, relevant_only=False)}
        rows = [row for row in store.all() if row["id"] in paper_ids]
    else:
        rows = store.all()
    scores_by_paper: dict[str, list] = {}
    for score in store.conn.execute("SELECT * FROM paper_topics ORDER BY topic_id"):
        scores_by_paper.setdefault(score["paper_id"], []).append(score)
    written = 0
    unchanged = 0
    for index, row in enumerate(rows, 1):
        content = render_note(store, row, topics, scores=scores_by_paper.get(row["id"], []))
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        destination = papers_dir / f"{_slug(row['id'])}.md"
        if row["obsidian_hash"] == digest and destination.exists():
            unchanged += 1
            continue
        destination.write_text(content, encoding="utf-8")
        store.set_obsidian(row["id"], str(destination), digest, commit=False)
        written += 1
        if index % 500 == 0:
            store.conn.commit()
            log(f"  [obsidian] {index}/{len(rows)} notes")
    store.conn.commit()
    base = _base_data(cfg.obsidian.folder, cfg.topics)
    (root / "PaperLab.base").write_text(
        yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8")
    dashboard = [
        "# PaperLab", "", f"Total papers: **{len(store.all())}**", "",
        "![[PaperLab.base#All Papers]]", "", "## Topic views", "",
    ]
    dashboard.extend(f"- ![[PaperLab.base#{topic.display_name}]]" for topic in cfg.topics)
    dashboard.extend([
        "", "## Commands", "", "```bash", "paperlab update",
        "paperlab score --topic kv-cache", "paperlab download --topic kv-cache",
        "paperlab obsidian sync", "```", "",
    ])
    (root / "README.md").write_text("\n".join(dashboard), encoding="utf-8")
    log(f"Obsidian sync: {written} written, {unchanged} unchanged -> {root}")
    return {"written": written, "unchanged": unchanged, "root": str(root)}
