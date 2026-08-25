import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paperlab_local.config import RelatedConfig
from paperlab_local.related import find_related
from paperlab_local.store import Store


class _Config:
    related = RelatedConfig(result_limit=10, company_aliases={
        "NVIDIA": ["nvidia"], "Anthropic": ["anthropic", "claude"]
    })


class RelatedPaperTests(unittest.TestCase):
    def test_company_boost_ranking_dedup_and_existing_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "papers.db"
            with Store(db) as store:
                seed = store.upsert_candidate({"title": "Seed", "doi": "10.1/seed"})
                store.upsert_candidate({"title": "Already Here"})
            raw = [
                {"title": "Popular", "citation_count": 100, "organizations": "University", "relation": "reference"},
                {"title": "Industry", "citation_count": 20, "organizations": "NVIDIA Research", "relation": "citation"},
                {"title": "Already Here", "citation_count": 5, "organizations": "", "relation": "reference"},
                {"title": "Industry", "citation_count": 20, "organizations": "NVIDIA Research", "relation": "reference"},
            ]
            with patch("paperlab_local.related._openalex_related", return_value=raw):
                result = find_related(db, _Config(), seed)
            self.assertEqual(result["candidates"][0]["title"], "Industry")
            self.assertEqual(result["candidates"][0]["relation"], "citation,reference")
            existing = next(x for x in result["candidates"] if x["title"] == "Already Here")
            self.assertTrue(existing["in_library"])


if __name__ == "__main__":
    unittest.main()
