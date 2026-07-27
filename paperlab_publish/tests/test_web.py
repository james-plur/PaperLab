import tempfile
import unittest
from pathlib import Path

from paperlab_publish.store import Store
from paperlab_publish.web import HTML, query_facets, query_papers, reconcile_pdf_paths


class TestWebQueries(unittest.TestCase):
    def test_reconcile_pdf_paths_restores_migrated_file_link(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "papers.db"
            with Store(db) as store:
                paper_id = store.upsert_candidate({"title": "Portable PDF"})
            pdf_dir = root / "pdfs"
            pdf_dir.mkdir()
            target = pdf_dir / (paper_id.replace(":", "-").replace("/", "_") + ".pdf")
            target.write_bytes(b"%PDF-1.7\n" + b"x" * 1200)
            self.assertEqual(reconcile_pdf_paths(db, root), 1)
            with Store(db, backup_on_migrate=False) as store:
                self.assertEqual(Path(store.get(paper_id)["pdf_path"]), target.resolve())

    def test_missing_report_triggers_generation(self):
        self.assertIn('else if(r.ok&&d.status==="none")', HTML)
        self.assertIn('await generateReport(id);', HTML)
        self.assertIn('>Open report</button>', HTML)
        self.assertIn('>Generate report</button>', HTML)
        self.assertIn('S.topicReviewAvailable?"Open review":"Generate review"', HTML)
        self.assertIn('>Regenerate review</button>', HTML)

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
            self.assertFalse(result["papers"][0]["has_report"])
            facets = query_facets(db)
            self.assertEqual(facets["total"], 2)
            self.assertEqual(facets["award_count"], 1)

    def test_report_availability_requires_valid_report_file_not_pdf(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "papers.db"
            report_file = root / "report.md"
            report_file.write_text("# Report", encoding="utf-8")
            with Store(db) as store:
                ready = store.upsert_candidate({"title": "Ready report"})
                missing = store.upsert_candidate({"title": "No report"})
                store.set_report(ready, report_path=str(report_file))
            ready_result = query_papers(db, {"q": ["Ready report"]})["papers"][0]
            missing_result = query_papers(db, {"q": ["No report"]})["papers"][0]
            self.assertTrue(ready_result["has_report"])
            self.assertIsNone(ready_result["pdf_path"])
            self.assertFalse(missing_result["has_report"])


if __name__ == "__main__":
    unittest.main()
