import os
import tempfile
import unittest

from paperlab_local.store import Store, compute_id, normalize_title, title_hash


class TestIDs(unittest.TestCase):
    def test_arxiv_preferred(self):
        self.assertEqual(compute_id(arxiv_id="2401.01234"), "arxiv:2401.01234")

    def test_doi_when_no_arxiv(self):
        self.assertEqual(compute_id(doi="10.1145/123.456"), "doi:10.1145/123.456")

    def test_title_hash_fallback(self):
        pid = compute_id(title="Cool System: A Paper!")
        self.assertEqual(pid, "thash:" + title_hash("Cool System: A Paper!"))

    def test_normalization_strips_punctuation(self):
        self.assertEqual(normalize_title("Cool  System: A Paper!"),
                         "cool system a paper")


class TestDedup(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.mkdtemp()
        self.store = Store(os.path.join(self._d, "t.db"))

    def tearDown(self):
        self.store.close()

    def test_venue_and_arxiv_merge(self):
        venue = {"title": "FooBar System",
                 "authors": "A. Author", "venue": "OSDI", "year": 2025}
        arxiv = {"title": "FooBar System", "authors": "A. Author, B. Bee",
                 "arxiv_id": "2501.00001", "abstract": "An abstract.",
                 "venue": "arXiv", "year": 2025}
        vid = self.store.upsert_candidate(venue, "llm")
        aid = self.store.upsert_candidate(arxiv, "llm")
        self.assertEqual(vid, aid)  # same row: arxiv id wins
        row = self.store.get(vid)
        self.assertEqual(row["arxiv_id"], "2501.00001")
        self.assertIn("llm", row["topics"])
        self.assertEqual(row["venue"], "OSDI")   # venue backfilled
        self.assertEqual(row["abstract"], "An abstract.")

    def test_topic_union_on_reimport(self):
        a = {"title": "Paper X", "venue": "ISCA", "year": 2024}
        pid = self.store.upsert_candidate(a, "topicA")
        self.store.upsert_candidate(a, "topicB")
        row = self.store.get(pid)
        self.assertEqual(row["topics"], "topicA,topicB")

    def test_pending_stages(self):
        a = {"title": "Paper Y", "venue": "ISCA", "year": 2024}
        pid = self.store.upsert_candidate(a, "t")
        self.assertEqual(len(self.store.pending("t", "relevance")), 1)
        self.store.set_relevance(pid, 0.9)
        self.assertEqual(len(self.store.pending("t", "relevance")), 0)
        self.assertEqual(len(self.store.pending("t", "download")), 1)


if __name__ == "__main__":
    unittest.main()
