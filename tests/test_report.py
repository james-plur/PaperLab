"""Tests for paper_reports table and report generation plumbing."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from paperlab.store import Store


class TestReportStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "test.db"
        self.store = Store(self.db)
        self.paper_id = self.store.upsert_candidate({
            "title": "Test Paper for Report",
            "venue": "TEST",
            "year": 2024,
            "abstract": "An abstract.",
            "source": "test",
        })

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_set_and_get_report(self):
        self.store.set_report(self.paper_id, report_path="/tmp/report.md", status="ok")
        rpt = self.store.get_report(self.paper_id)
        self.assertIsNotNone(rpt)
        self.assertEqual(rpt["report_path"], "/tmp/report.md")
        self.assertEqual(rpt["status"], "ok")

    def test_update_report(self):
        self.store.set_report(self.paper_id, report_path="/tmp/v1.md", status="ok")
        self.store.set_report(self.paper_id, report_path="/tmp/v2.md", status="ok")
        rpt = self.store.get_report(self.paper_id)
        self.assertEqual(rpt["report_path"], "/tmp/v2.md")

    def test_get_report_none(self):
        rpt = self.store.get_report("nonexistent-id")
        self.assertIsNone(rpt)


class TestGenerateReport(unittest.TestCase):
    """Test that generate_report calls LLM and writes report to disk."""

    def test_generate_requires_pdf(self):
        from paperlab.report import generate_report
        from paperlab.config import Config, ModelConfig, CatalogConfig, MCPConfig, ObsidianConfig, ScheduleConfig
        import dataclasses

        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "test.db"
            with Store(db) as store:
                pid = store.upsert_candidate({
                    "title": "No PDF", "venue": "T", "year": 2024, "source": "test"})
                # Build a minimal config
                cfg = MagicMock()
                cfg.pdf_root = Path(d)
                cfg.models.extractor = "test-model"
                with self.assertRaises(RuntimeError):
                    generate_report(store, cfg, pid)

    def test_generate_writes_file(self):
        from paperlab.report import generate_report
        from unittest.mock import MagicMock

        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "test.db"
            # Create a fake PDF
            pdf_path = Path(d) / "fake.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 fake content for testing")

            with Store(db) as store:
                pid = store.upsert_candidate({
                    "title": "Has PDF", "venue": "T", "year": 2024, "source": "test",
                    "pdf_path": str(pdf_path),
                })
                # Manually set pdf_path since upsert_candidate doesn't store it
                store.set_pdf(pid, path=str(pdf_path), status="ok")

                cfg = MagicMock()
                cfg.pdf_root = Path(d)
                cfg.models.extractor = "test-model"

                with patch("paperlab.report.chat_text", return_value="# Has PDF\n\n## Background\nTest."):
                    with patch("paperlab.report.pdf_mod.extract_full_text", return_value="full text here"):
                        path = generate_report(store, cfg, pid)
                        self.assertTrue(os.path.isfile(path))
                        content = open(path).read()
                        self.assertIn("Has PDF", content)
                        self.assertIn("Background", content)

                        # Verify DB has the path
                        rpt = store.get_report(pid)
                        self.assertIsNotNone(rpt)
                        self.assertEqual(rpt["report_path"], path)


if __name__ == "__main__":
    unittest.main()
