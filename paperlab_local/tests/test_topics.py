import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from paperlab_local.report import generate_topic_review, topic_snapshot_hash
from paperlab_local.store import Store


class TopicStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "papers.db"
        self.store = Store(self.db, backup_on_migrate=False)
        self.paper_id = self.store.upsert_candidate({"title": "A Paper", "year": 2024})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_topic_crud_membership_and_cascade(self):
        topic = self.store.create_collection_topic("  Systems  ")
        self.assertEqual(topic["name"], "Systems")
        self.assertTrue(self.store.add_paper_to_collection(topic["id"], self.paper_id))
        self.assertFalse(self.store.add_paper_to_collection(topic["id"], self.paper_id))
        self.assertEqual([r["id"] for r in self.store.collection_papers(topic["id"])], [self.paper_id])
        self.store.set_topic_review(topic["id"], status="ok", review_path="/tmp/review.md")
        deleted = self.store.delete_collection_topic(topic["id"])
        self.assertIsNotNone(deleted)
        self.assertIsNotNone(self.store.get(self.paper_id))
        self.assertIsNone(self.store.get_topic_review(topic["id"]))

    def test_name_validation_and_case_insensitive_uniqueness(self):
        with self.assertRaises(ValueError):
            self.store.create_collection_topic("  ")
        self.store.create_collection_topic("Memory")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_collection_topic("memory")

    def test_membership_change_marks_existing_review_stale(self):
        topic = self.store.create_collection_topic("Storage")
        self.store.set_topic_review(topic["id"], status="ok", review_path="/tmp/review.md")
        self.store.add_paper_to_collection(topic["id"], self.paper_id)
        self.assertEqual(self.store.get_topic_review(topic["id"])["status"], "stale")


class TopicReviewTests(unittest.TestCase):
    def test_existing_reports_are_combined_and_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = SimpleNamespace(db_path=root / "papers.db", pdf_root=root)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\n" + b"0" * 1200)
            report = root / "single.md"
            report.write_text("# Single report\nmethod details", encoding="utf-8")
            with Store(cfg.db_path, backup_on_migrate=False) as store:
                pid = store.upsert_candidate({"title": "Early Work", "year": 2020})
                store.set_pdf(pid, path=str(pdf), status="ok")
                store.set_report(pid, report_path=str(report))
                topic = store.create_collection_topic("Caching")
                store.add_paper_to_collection(topic["id"], pid)
                with patch("paperlab_local.report.chat_text", return_value="# Caching\n\n## 核心问题\n...") as chat:
                    path = generate_topic_review(store, cfg, topic["id"])
                self.assertTrue(Path(path).is_file())
                review = store.get_topic_review(topic["id"])
                self.assertEqual(review["status"], "ok")
                self.assertEqual(review["snapshot_hash"], topic_snapshot_hash([pid]))
                prompt = chat.call_args.args[1]
                self.assertIn("Early Work", prompt)
                self.assertIn("method details", prompt)

    def test_empty_topic_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = SimpleNamespace(db_path=root / "papers.db", pdf_root=root)
            with Store(cfg.db_path, backup_on_migrate=False) as store:
                topic = store.create_collection_topic("Empty")
                with self.assertRaisesRegex(ValueError, "no papers"):
                    generate_topic_review(store, cfg, topic["id"])

    def test_missing_report_is_rejected_without_generation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = SimpleNamespace(db_path=root / "papers.db", pdf_root=root)
            with Store(cfg.db_path, backup_on_migrate=False) as store:
                pid = store.upsert_candidate({"title": "Needs Report", "year": 2024})
                topic = store.create_collection_topic("Incomplete")
                store.add_paper_to_collection(topic["id"], pid)
                with patch("paperlab_local.report.generate_report") as generate, \
                     patch("paperlab_local.report.chat_text") as chat:
                    with self.assertRaisesRegex(ValueError, "missing reports"):
                        generate_topic_review(store, cfg, topic["id"])
                generate.assert_not_called()
                chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
