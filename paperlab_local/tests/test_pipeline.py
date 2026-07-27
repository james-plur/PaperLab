import os
import tempfile
import unittest
from unittest import mock

from paperlab_local import pipeline
from paperlab_local.config import ModelConfig, TopicConfig
from paperlab_local.store import Store
from pathlib import Path



class _Cfg:
    def __init__(self):
        self.models = ModelConfig()


def _topic(name="t", relevance_threshold=0.5, pdf_dir=None):
    return TopicConfig(
        name=name, keywords=["k"], arxiv_categories=["cs.DC"], venues=[],
        year_range=(2024, 2026), citation_threshold=0,
        relevance_threshold=relevance_threshold, pdf_dir=Path(pdf_dir),
    )


class TestDownloadResolution(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.mkdtemp()
        self.store = Store(os.path.join(self._d, "t.db"))
        self.cfg = _Cfg()
        self.topic = _topic(pdf_dir=os.path.join(self._d, "pdfs"))

    def tearDown(self):
        self.store.close()

    def _seed(self, cand):
        self.store.upsert_candidate(cand, "t")
        for r in self.store.topic_papers("t"):
            self.store.set_relevance(r["id"], 0.9)
        return self.store.pending("t", "download")[0]

    def test_arxiv_first(self):
        r = self._seed({"title": "P1", "arxiv_id": "2401.00001", "venue": "arXiv", "year": 2024})
        with mock.patch.object(pipeline.pdf_mod, "download") as dl, \
             mock.patch.object(pipeline.s2, "match_by_title") as m2:
            pipeline.download(self.store, self.cfg, self.topic, log=lambda *_: None)
            m2.assert_not_called()
            self.assertTrue(dl.called)
            url = dl.call_args[0][0]
            self.assertIn("arxiv.org/pdf/2401.00001", url)
        row = self.store.get(r["id"])
        self.assertEqual(row["pdf_status"], "ok")

    def test_oa_fallback_when_no_arxiv(self):
        r = self._seed({"title": "P2", "venue": "OSDI", "year": 2024})
        with mock.patch.object(pipeline.s2, "match_by_title",
                               return_value={"arxiv_id": None, "pdf_url": "https://oa/p2.pdf",
                                             "abstract": "x"}), \
             mock.patch.object(pipeline.pdf_mod, "download") as dl:
            pipeline.download(self.store, self.cfg, self.topic, log=lambda *_: None)
            self.assertTrue(dl.called)
            self.assertEqual(dl.call_args[0][0], "https://oa/p2.pdf")
        self.assertEqual(self.store.get(r["id"])["pdf_status"], "ok")

    def test_unavailable_when_nothing(self):
        r = self._seed({"title": "P3", "venue": "OSDI", "year": 2024})
        with mock.patch.object(pipeline.s2, "match_by_title", return_value=None), \
             mock.patch.object(pipeline.pdf_mod, "download") as dl:
            pipeline.download(self.store, self.cfg, self.topic, log=lambda *_: None)
            dl.assert_not_called()
        self.assertEqual(self.store.get(r["id"])["pdf_status"], "unavailable")


if __name__ == "__main__":
    unittest.main()


class TestSemanticScholarEnrich(unittest.TestCase):
    """Direct S2 Graph API enrich fills abstracts without the MCP server."""

    def setUp(self):
        self._d = tempfile.mkdtemp()
        self.store = Store(os.path.join(self._d, "t.db"))

    def tearDown(self):
        self.store.close()

    def test_doi_lookup_fills_abstract(self):
        r = self.store.upsert_candidate(
            {"title": "Forest: Access-aware GPU UVM Management.",
             "doi": "10.1145/3695053.3731047", "venue": "ISCA", "year": 2025})
        with mock.patch.object(pipeline.s2, "get_batch_strict",
                               return_value={"DOI:10.1145/3695053.3731047":
                                             {"abstract": "UVM is great.",
                                              "doi": "10.1145/3695053.3731047",
                                              "arxiv_id": None, "authors": "A. B",
                                              "citation_count": 9, "pdf_url": "",
                                              "year": 2025}}), \
             mock.patch.object(pipeline.s2, "match_by_title_strict") as m2:
            n = pipeline.enrich_semantic_scholar(self.store, log=lambda *_: None)
        self.assertEqual(n, 1)
        m2.assert_not_called()
        row = self.store.get(r)
        self.assertEqual(row["abstract"], "UVM is great.")
        self.assertEqual(row["metadata_status"], "complete")

    def test_batch_doi_lookups_fill_multiple_papers(self):
        """Two DOI-backed papers are resolved in a single batch call."""
        r1 = self.store.upsert_candidate(
            {"title": "Paper One", "doi": "10.1/a", "venue": "ISCA", "year": 2025})
        r2 = self.store.upsert_candidate(
            {"title": "Paper Two", "doi": "10.2/b", "venue": "MICRO", "year": 2025})
        batch_return = {
            "DOI:10.1/a": {"abstract": "Abstract one.",
                           "doi": "10.1/a", "arxiv_id": None,
                           "authors": "X", "citation_count": 1,
                           "pdf_url": "", "year": 2025},
            "DOI:10.2/b": {"abstract": "Abstract two.",
                           "doi": "10.2/b", "arxiv_id": "2501.00002",
                           "authors": "Y", "citation_count": 2,
                           "pdf_url": "https://oa.com/two.pdf", "year": 2025},
        }
        with mock.patch.object(pipeline.s2, "get_batch_strict",
                               return_value=batch_return) as mb, \
             mock.patch.object(pipeline.s2, "match_by_title_strict") as mt:
            n = pipeline.enrich_semantic_scholar(self.store, log=lambda *_: None)
        self.assertEqual(n, 2)
        mb.assert_called_once()
        # Title fallback is never needed when batch resolves everything.
        mt.assert_not_called()
        self.assertEqual(self.store.get(r1)["abstract"], "Abstract one.")
        self.assertEqual(self.store.get(r2)["arxiv_id"], "2501.00002")
        self.assertEqual(self.store.get(r2)["pdf_url"], "https://oa.com/two.pdf")

    def test_batch_miss_falls_back_to_title(self):
        """A DOI that S2 doesn't know about gets a title-match attempt."""
        r = self.store.upsert_candidate(
            {"title": "Unknown DOI Paper",
             "doi": "10.3/missing", "venue": "ISCA", "year": 2025})
        with mock.patch.object(pipeline.s2, "get_batch_strict",
                               return_value={"DOI:10.3/missing": None}), \
             mock.patch.object(pipeline.s2, "match_by_title_strict",
                               return_value={"abstract": "Found by title.",
                                             "doi": None, "arxiv_id": None,
                                             "authors": "", "citation_count": 0,
                                             "pdf_url": "", "year": 2025,
                                             "title": "Unknown DOI Paper"}):
            n = pipeline.enrich_semantic_scholar(self.store, log=lambda *_: None)
        self.assertEqual(n, 1)
        self.assertEqual(self.store.get(r)["abstract"], "Found by title.")

    def test_batch_transient_leaves_retryable(self):
        """A transient batch error does not record a miss."""
        self.store.upsert_candidate(
            {"title": "Transient Paper", "doi": "10.4/transient",
             "venue": "ISCA", "year": 2025})
        with mock.patch.object(pipeline.s2, "get_batch_strict",
                               side_effect=pipeline.s2.TransientError("429")), \
             mock.patch.object(pipeline.s2, "match_by_title_strict") as mt:
            n = pipeline.enrich_semantic_scholar(self.store, log=lambda *_: None)
        self.assertEqual(n, 0)
        # No miss recorded -- the row should be retried on the next run.
        miss = self.store.conn.execute(
            "SELECT count(*) FROM paper_sources WHERE source='semantic-scholar-miss'"
        ).fetchone()[0]
        self.assertEqual(miss, 0)
        # Title fallback should not be attempted when the batch was transient.
        mt.assert_not_called()

    def test_title_fallback_for_doi_less_paper(self):
        r = self.store.upsert_candidate(
            {"title": "CODO: An Automated Compiler for Comprehensive Dataflow Optimization",
             "venue": "ISCA", "year": 2026})
        with mock.patch.object(pipeline.s2, "get_by_doi_strict", return_value=None), \
             mock.patch.object(pipeline.s2, "match_by_title_strict",
                               return_value={"abstract": "Dataflow compiler.",
                                             "arxiv_id": "2604.12618",
                                             "doi": None, "authors": "",
                                             "citation_count": 0, "pdf_url": "",
                                             "year": 2026,
                                             "title": "CODO: An Automated Compiler for Comprehensive Dataflow Optimization"}):
            n = pipeline.enrich_semantic_scholar(self.store, log=lambda *_: None)
        self.assertEqual(n, 1)
        row = self.store.get(r)
        self.assertEqual(row["abstract"], "Dataflow compiler.")
        self.assertEqual(row["arxiv_id"], "2604.12618")

    def test_low_similarity_match_is_skipped(self):
        self.store.upsert_candidate(
            {"title": "A Very Different Title That Should Not Match",
             "venue": "ISCA", "year": 2026})
        with mock.patch.object(pipeline.s2, "get_by_doi_strict", return_value=None), \
             mock.patch.object(pipeline.s2, "match_by_title_strict",
                               return_value={"abstract": "wrong", "title": "Some Other Paper"}):
            n = pipeline.enrich_semantic_scholar(self.store, log=lambda *_: None)
        self.assertEqual(n, 0)
        # A miss is recorded so the row is not retried on the next run.
        miss = self.store.conn.execute(
            "SELECT count(*) FROM paper_sources WHERE source='semantic-scholar-miss'").fetchone()[0]
        self.assertEqual(miss, 1)

    def test_missed_paper_not_retried(self):
        self.store.upsert_candidate(
            {"title": "Unfindable Paper", "venue": "ISCA", "year": 2026})
        calls = {"match": 0}

        def fake_match(title):
            calls["match"] += 1
            return None

        with mock.patch.object(pipeline.s2, "get_by_doi_strict", return_value=None), \
             mock.patch.object(pipeline.s2, "match_by_title_strict", side_effect=fake_match):
            pipeline.enrich_semantic_scholar(self.store, log=lambda *_: None)
            pipeline.enrich_semantic_scholar(self.store, log=lambda *_: None)
        self.assertEqual(calls["match"], 1)
