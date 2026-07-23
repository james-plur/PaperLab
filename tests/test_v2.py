import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from paperlab import obsidian, relevance, schedule
from paperlab.config import (
    CatalogConfig, Config, MCPConfig, ModelConfig, ObsidianConfig,
    ScheduleConfig, TopicConfig,
)
from paperlab.mcp_client import PaperSearchMCP
from paperlab.store import Store
from paperlab.sources.openalex_bulk import _parse_work, reconstruct_abstract
from paperlab.venues import ai


def _topic(name="kv-cache", threshold=0.5):
    return TopicConfig(
        name=name, display_name=name, description="cache relevance", keywords=[],
        arxiv_categories=[], venues=[], year_range=(2022, 2026),
        citation_threshold=0, relevance_threshold=threshold,
        pdf_dir=Path("/tmp/pdfs"),
    )


def _cfg(root: Path, vault: Path):
    topics = [_topic("kv-cache"), _topic("ai-network")]
    return Config(
        models=ModelConfig(scorer="test-model"), topics=topics, root=root,
        db_path=root / "papers.db", site_dir=root / "site",
        catalog=CatalogConfig(["CVPR"], (2022, 2026)),
        mcp=MCPConfig("uvx", ["paper-search-mcp"], ["arxiv"]),
        obsidian=ObsidianConfig(vault, "论文/PaperLab"),
        schedule=ScheduleConfig(), pdf_root=root / "pdfs",
    )


class TestTopicRelations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "papers.db")
        self.paper_id = self.store.upsert_candidate({
            "title": "Paged KV Cache", "authors": "A; B", "venue": "SOSP",
            "year": 2025, "abstract": "A cache system.",
        })

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_scores_are_independent(self):
        self.store.set_topic_score(
            self.paper_id, "kv-cache", 0.9, "direct", threshold=0.5,
            model="m", definition_hash="a", prompt_version=2)
        self.store.set_topic_score(
            self.paper_id, "ai-network", 0.2, "not network", threshold=0.5,
            model="m", definition_hash="b", prompt_version=2)
        scores = {r["topic_id"]: r for r in self.store.topic_scores(self.paper_id)}
        self.assertEqual(scores["kv-cache"]["relevant"], 1)
        self.assertEqual(scores["ai-network"]["relevant"], 0)

    def test_definition_change_becomes_pending(self):
        self.store.set_topic_score(
            self.paper_id, "kv-cache", 0.9, "direct", threshold=0.5,
            model="m", definition_hash="old", prompt_version=2)
        self.assertEqual(self.store.pending_topic("kv-cache", "old", 2), [])
        self.assertEqual(len(self.store.pending_topic("kv-cache", "new", 2)), 1)


class TestObsidianSync(unittest.TestCase):
    def test_sync_is_idempotent_and_valid_yaml(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault = root / "vault"
            cfg = _cfg(root, vault)
            with Store(root / "papers.db") as store:
                paper_id = store.upsert_candidate({
                    "title": "Serving: A Study", "authors": "Alice; Bob",
                    "organizations": "MIT; Stanford", "venue": "OSDI", "year": 2025,
                    "abstract": "Abstract text.",
                })
                store.set_topic_score(
                    paper_id, "kv-cache", 0.8, "direct", threshold=0.5,
                    model="m", definition_hash="d", prompt_version=2)
                first = obsidian.sync(store, cfg, log=lambda *_: None)
                second = obsidian.sync(store, cfg, log=lambda *_: None)
                self.assertEqual(first["written"], 1)
                self.assertEqual(second["unchanged"], 1)
                note = next((vault / "论文/PaperLab/Papers").glob("*.md"))
                frontmatter = note.read_text(encoding="utf-8").split("---", 2)[1]
                props = yaml.safe_load(frontmatter)
                self.assertIn("paper/topic/kv-cache", props["tags"])
                self.assertEqual(props["score_kv_cache"], 0.8)
                base = yaml.safe_load((vault / "论文/PaperLab/PaperLab.base").read_text())
                self.assertTrue(any(v["name"] == "kv-cache" for v in base["views"]))


class TestScoring(unittest.TestCase):
    def test_multi_topic_parser(self):
        rows = [{"id": "p1", "title": "KV transfer", "abstract": "cache over RDMA", "venue": "NSDI"}]
        topics = [_topic("kv-cache"), _topic("ai-network")]
        result = {"papers": [{"id": "p1", "topics": {
            "kv-cache": {"score": 0.9, "reason": "cache"},
            "ai-network": {"score": 0.8, "reason": "RDMA"},
        }}]}
        with mock.patch.object(relevance, "chat_json", return_value=result):
            parsed = relevance.score_multi_batch(rows, topics, model="m")
        self.assertEqual(parsed["p1"]["kv-cache"][0], 0.9)
        self.assertEqual(parsed["p1"]["ai-network"][0], 0.8)


class TestMCPAndSchedule(unittest.TestCase):
    def test_structured_mcp_result_unwrap(self):
        result = PaperSearchMCP._unwrap({"structuredContent": {"result": {"papers": [1]}}})
        self.assertEqual(result, {"papers": [1]})

    def test_launchagent_plist(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = _cfg(root, root / "vault")
            data = schedule.build_plist(cfg, str(root / "topics.yaml"))
            encoded = plistlib.dumps(data)
            decoded = plistlib.loads(encoded)
            self.assertEqual(decoded["StartCalendarInterval"]["Weekday"], 1)
            self.assertIn(" update", decoded["ProgramArguments"][-1])


class TestAIVenues(unittest.TestCase):
    def test_neurips_main_track_parser(self):
        raw = '''<ul class="paper-list"><li class="conference" data-track="conference">
        <div class="paper-content"><a title="paper title" href="/paper/x.html">A Great Paper</a>
        <span class="paper-authors">Alice, Bob</span></div></li></ul>'''
        with mock.patch.object(ai, "_get", return_value=raw):
            papers = ai._official_neurips(2025)
        self.assertEqual(papers[0]["title"], "A Great Paper")
        self.assertEqual(papers[0]["authors"], "Alice, Bob")

    def test_dblp_stream_paginates_and_preserves_metadata(self):
        first = '{"result":{"hits":{"@total":"2","hit":[{"info":{' \
                '"title":"Paper One.","authors":{"author":{"text":"Alice"}},' \
                '"doi":"10.1/one","key":"conf/eccv/one","url":"u1"}}]}}}'
        second = '{"result":{"hits":{"@total":"2","hit":[{"info":{' \
                 '"title":"Paper Two.","authors":{"author":[{"text":"Bob"}]},' \
                 '"ee":"https://doi.org/10.1/two","key":"conf/eccv/two"}}]}}}'
        with mock.patch.object(ai, "_get", side_effect=[first, second]):
            papers = ai._dblp_stream("ECCV", 2024)
        self.assertEqual([p["title"] for p in papers], ["Paper One", "Paper Two"])
        self.assertEqual(papers[1]["doi"], "10.1/two")
        self.assertEqual(papers[0]["source_id"], "conf/eccv/one")

    def test_eccv_virtual_preserves_abstract_and_institutions(self):
        raw = '''{"results":[{"id":1,"uid":"u1","name":"Vision Paper",
        "authors":[{"fullname":"Alice","institution":"MIT"},
        {"fullname":"Bob","institution":"MIT"}],"abstract":"An abstract.",
        "virtualsite_url":"/virtual/2024/poster/1","paper_pdf_url":"https://x/p.pdf",
        "event_type":"Poster"}]}'''
        paper = ai._parse_eccv_virtual(raw, 2024)[0]
        self.assertEqual(paper["organizations"], "MIT")
        self.assertEqual(paper["abstract"], "An abstract.")
        self.assertEqual(paper["pdf_url"], "https://x/p.pdf")

    def test_ecva_archive_parses_doi_and_pdf(self):
        raw = '''<!-- ECCV 2022 --><div><dl>
        <dt class="ptitle"><br><a href=papers/eccv_2022/html/1.php>A Paper</a></dt>
        <dd>Alice, Bob</dd><dd>[<a href='papers/eccv_2022/1.pdf'>pdf</a>]
        <a href="https://link.springer.com/chapter/10.1007/test">DOI</a></dd>
        </dl></div><!-- ECCV 2020 -->'''
        paper = ai._parse_ecva_archive(raw, 2022)[0]
        self.assertEqual(paper["doi"], "10.1007/test")
        self.assertTrue(paper["pdf_url"].endswith("/papers/eccv_2022/1.pdf"))


class TestOpenAlexBulk(unittest.TestCase):
    def test_reconstructs_abstract_and_affiliations(self):
        work = {
            "id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/x",
            "title": "Paper", "abstract_inverted_index": {"cache": [1], "KV": [0]},
            "authorships": [{"author": {"display_name": "Alice"},
                             "institutions": [{"display_name": "MIT"}]}],
            "primary_location": {"landing_page_url": "https://example.test/p",
                                 "pdf_url": "https://example.test/p.pdf"},
            "open_access": {}, "cited_by_count": 3,
        }
        parsed = _parse_work(work)
        self.assertEqual(reconstruct_abstract(work["abstract_inverted_index"]), "KV cache")
        self.assertEqual(parsed["organizations"], "MIT")
        self.assertEqual(parsed["doi"], "10.1/x")
        self.assertEqual(parsed["metadata_status"], "complete")


if __name__ == "__main__":
    unittest.main()
