import tempfile
import unittest
from pathlib import Path

from paperlab.store import Store
from paperlab.web import query_facets, query_papers


class TestWebQueries(unittest.TestCase):
    def test_paginated_search_filters_and_awards(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "papers.db"
            with Store(db) as store:
                mooncake = store.upsert_candidate({
                    "title": "Mooncake", "authors": "Alice", "venue": "FAST",
                    "year": 2025, "abstract": "KV cache serving",
                    "organizations": "Moonshot AI", "awards": "Awarded Best Paper!",
                })
                store.upsert_candidate({"title": "Other", "venue": "OSDI", "year": 2024})
                store.set_topic_score(
                    mooncake, "kv-cache", 0.9, "direct", threshold=0.5,
                    model="m", definition_hash="d", prompt_version=2,
                )
            result = query_papers(db, {"q": ["Mooncake"], "award": ["1"]})
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["papers"][0]["venue"], "FAST")
            self.assertIn("kv-cache", result["papers"][0]["topics"])
            facets = query_facets(db)
            self.assertEqual(facets["total"], 2)
            self.assertEqual(facets["award_count"], 1)


if __name__ == "__main__":
    unittest.main()
