"""PaperLab configuration loading and validation.

The v2 configuration separates the conference catalogue from relevance topics:
every catalogue paper is collected once, then independently scored for every
topic.  The loader remains backward compatible with the original topics.yaml.
"""
from __future__ import annotations

import dataclasses
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import yaml


DEFAULT_VENUES = [
    "NEURIPS", "ICML", "ICLR", "AAAI", "IJCAI", "KDD", "MLSYS",
    "ACL", "EMNLP", "NAACL", "CVPR", "ICCV", "ECCV",
    "ISCA", "MICRO", "HPCA", "ASPLOS", "SIGCOMM", "NSDI",
    "INFOCOM", "FAST", "OSDI", "SOSP",
]

DEFAULT_TOPICS = [
    {
        "name": "ai-infra",
        "display_name": "AI infra",
        "description": (
            "Systems and infrastructure for AI model training, inference and serving, "
            "including accelerators, scheduling, memory, storage and distributed execution."
        ),
    },
    {
        "name": "ai-agent",
        "display_name": "AI application (agent)",
        "description": (
            "LLM agents, tool use, planning, multi-agent systems, agent workflows, "
            "retrieval and long-term agent memory."
        ),
    },
    {
        "name": "ai-network",
        "display_name": "AI Network",
        "description": (
            "Networking required by AI clusters and large-model training or inference: "
            "interconnects, collectives, RDMA, congestion control, topology and communication scheduling. "
            "Exclude applying AI to conventional network management."
        ),
    },
    {
        "name": "kv-cache",
        "display_name": "KV cache",
        "description": (
            "Key-value cache management for model inference, including compression, "
            "offload, reuse, scheduling, eviction and tiered storage."
        ),
    },
]


@dataclasses.dataclass
class ModelConfig:
    scorer: str = "gpt-4.1-mini"
    extractor: str = "gpt-4.1"
    figure: str = "gpt-4.1"


@dataclasses.dataclass
class TopicConfig:
    name: str
    keywords: list[str]
    arxiv_categories: list[str]
    venues: list[str]
    year_range: tuple[int, int]
    citation_threshold: int
    relevance_threshold: float
    pdf_dir: Path
    display_name: str = ""
    description: str = ""

    @property
    def threshold(self) -> float:
        return self.relevance_threshold


@dataclasses.dataclass
class CatalogConfig:
    venues: list[str]
    year_range: tuple[int, int]
    incremental_lookback_years: int = 1


@dataclasses.dataclass
class MCPConfig:
    command: str
    args: list[str]
    sources: list[str]
    timeout_seconds: int = 120


@dataclasses.dataclass
class ObsidianConfig:
    vault_path: Path
    folder: str = "论文/PaperLab"
    enabled: bool = False

    @property
    def output_dir(self) -> Path:
        return self.vault_path / self.folder


@dataclasses.dataclass
class ScheduleConfig:
    weekday: int = 1  # launchd: 1=Monday
    hour: int = 9
    minute: int = 0


@dataclasses.dataclass
class Config:
    models: ModelConfig
    topics: list[TopicConfig]
    root: Path
    db_path: Path
    site_dir: Path
    catalog: CatalogConfig
    mcp: MCPConfig
    obsidian: ObsidianConfig
    schedule: ScheduleConfig
    pdf_root: Path

    def topic(self, name: str) -> TopicConfig:
        normalized = name.strip().lower()
        for topic in self.topics:
            if topic.name.lower() == normalized:
                return topic
        raise KeyError(f"topic '{name}' not found in config")

    def figures_dir(self, topic_name: str) -> Path:
        return (self.pdf_root.parent / "figures" / topic_name).resolve()


def _default_year_range() -> list[int]:
    year = date.today().year
    return [year - 4, year]


def _active_obsidian_vault() -> Path:
    """Resolve the active vault without requiring the Obsidian CLI."""
    app_cfg = Path.home() / "Library/Application Support/obsidian/obsidian.json"
    try:
        import json
        data = json.loads(app_cfg.read_text(encoding="utf-8"))
        vaults = list((data.get("vaults") or {}).values())
        opened = [v for v in vaults if v.get("open")]
        candidates = opened or sorted(vaults, key=lambda v: v.get("ts", 0), reverse=True)
        if candidates and candidates[0].get("path"):
            return Path(candidates[0]["path"]).expanduser().resolve()
    except (OSError, ValueError, TypeError):
        pass
    return (Path.home() / "Documents/Obsidian").resolve()


def _find_uvx() -> str:
    found = shutil.which("uvx")
    if found:
        return found
    local = Path.home() / ".local/bin/uvx"
    return str(local if local.exists() else "uvx")


def load_config(path: str | Path = "topics.yaml") -> Config:
    config_path = Path(path).resolve()
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    root = config_path.parent

    model_raw = raw.get("models") or {}
    models = ModelConfig(
        scorer=model_raw.get("scorer", "gpt-4.1-mini"),
        extractor=model_raw.get("extractor", "gpt-4.1"),
        figure=model_raw.get("figure", "gpt-4.1"),
    )

    defaults = raw.get("defaults") or {}
    default_years = list(defaults.get("year_range") or _default_year_range())
    default_citations = int(defaults.get("citation_threshold", 0))
    default_threshold = float(defaults.get("relevance_threshold", 0.5))
    pdf_root_raw = raw.get("pdf_root")
    if pdf_root_raw:
        candidate = Path(pdf_root_raw).expanduser()
        papers_root = (candidate if candidate.is_absolute() else root / candidate).resolve()
    else:
        papers_root = (Path.home() / "papers/paperlab_publish").resolve()

    topic_items = raw.get("topics")
    # A config without topics receives the four agreed defaults.  Existing v1
    # configs are kept intact so users do not lose custom research topics.
    if not topic_items:
        topic_items = DEFAULT_TOPICS
    topics: list[TopicConfig] = []
    for item in topic_items:
        years = list(item.get("year_range") or default_years)
        pdf_raw = item.get("pdf_dir")
        pdf_dir = ((root / pdf_raw).resolve() if pdf_raw else papers_root / "pdfs")
        topics.append(TopicConfig(
            name=str(item["name"]),
            display_name=str(item.get("display_name") or item["name"]),
            description=str(item.get("description") or ""),
            keywords=[str(x) for x in item.get("keywords", [])],
            arxiv_categories=[str(x) for x in item.get("arxiv_categories", [])],
            venues=[str(x).upper() for x in item.get("venues", [])],
            year_range=(int(years[0]), int(years[1])),
            citation_threshold=int(item.get("citation_threshold", default_citations)),
            relevance_threshold=float(item.get("threshold", item.get("relevance_threshold", default_threshold))),
            pdf_dir=Path(pdf_dir).resolve(),
        ))

    catalog_raw = raw.get("catalog") or {}
    cat_years = list(catalog_raw.get("year_range") or default_years)
    legacy_venues = list(dict.fromkeys(
        venue for topic in topics for venue in topic.venues
    ))
    catalog = CatalogConfig(
        venues=[str(x).upper() for x in catalog_raw.get("venues", legacy_venues or DEFAULT_VENUES)],
        year_range=(int(cat_years[0]), int(cat_years[1])),
        incremental_lookback_years=max(0, int(catalog_raw.get("incremental_lookback_years", 1))),
    )

    mcp_raw = raw.get("mcp") or {}
    mcp = MCPConfig(
        command=str(mcp_raw.get("command") or _find_uvx()),
        args=[str(x) for x in mcp_raw.get("args", ["paper-search-mcp"])],
        sources=[str(x) for x in mcp_raw.get("sources", ["arxiv", "semantic", "openalex", "crossref", "dblp"])],
        timeout_seconds=int(mcp_raw.get("timeout_seconds", 120)),
    )

    obs_raw = raw.get("obsidian") or {}
    vault = Path(obs_raw.get("vault_path") or _active_obsidian_vault()).expanduser().resolve()
    obsidian = ObsidianConfig(
        vault_path=vault, folder=str(obs_raw.get("folder", "论文/PaperLab")),
        enabled=bool(obs_raw.get("enabled", False)),
    )

    sched_raw = raw.get("schedule") or {}
    schedule = ScheduleConfig(
        weekday=int(sched_raw.get("weekday", 1)),
        hour=int(sched_raw.get("hour", 9)),
        minute=int(sched_raw.get("minute", 0)),
    )

    db_path = (root / raw.get("db_path", "data/papers.db")).resolve()
    site_raw = raw.get("site_dir")
    site_dir = (root / site_raw).resolve() if site_raw else (papers_root / "site").resolve()
    return Config(
        models=models, topics=topics, root=root, db_path=db_path, site_dir=site_dir,
        catalog=catalog, mcp=mcp, obsidian=obsidian, schedule=schedule,
        pdf_root=papers_root,
    )
