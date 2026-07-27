import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from paperlab_local.paper_import import (
    ImportError, MAX_PDF_BYTES, cleanup_previews, finalize_import, preview_pdf,
)
from paperlab_local.store import Store


def sample_pdf(title="Imported Systems Paper"):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), title, fontsize=20)
    page.insert_text((72, 110), "Alice Example, Bob Example", fontsize=11)
    page.insert_text((72, 150), "Abstract: This paper presents a fast cache system for AI serving.\n1 Introduction", fontsize=11)
    doc.set_metadata({"title": title, "author": "Alice Example; Bob Example"})
    value = doc.tobytes()
    doc.close()
    return value


class PaperImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "data" / "papers.db"
        self.db.parent.mkdir()
        with Store(self.db):
            pass

    def tearDown(self):
        self.temp.cleanup()

    def test_preview_combines_local_and_llm_fields(self):
        with patch("paperlab_local.paper_import._llm_fields", return_value={
            "title": "Imported Systems Paper", "authors": "Alice Example, Bob Example",
            "organizations": "Example University", "abstract": "A cache system.",
            "keywords": "KV cache, inference", "doi": None, "arxiv_id": None,
            "venue": "OSDI", "year": 2026, "source_url": None, "pdf_url": None,
        }):
            result = preview_pdf(self.db, sample_pdf())
        self.assertTrue(result["import_token"])
        self.assertEqual(result["fields"]["organizations"], "Example University")
        self.assertEqual(result["sources"]["keywords"], "llm")

    def test_llm_failure_keeps_local_preview(self):
        with patch("paperlab_local.paper_import._llm_fields", side_effect=RuntimeError("offline")):
            result = preview_pdf(self.db, sample_pdf())
        self.assertEqual(result["fields"]["title"], "Imported Systems Paper")
        self.assertIn("LLM extraction unavailable", result["warnings"][0])

    def test_rejects_non_pdf_and_oversized_payload(self):
        with self.assertRaisesRegex(ImportError, "valid PDF"):
            preview_pdf(self.db, b"not a pdf")
        with self.assertRaisesRegex(ImportError, "50 MB"):
            preview_pdf(self.db, b"%PDF-" + b"x" * MAX_PDF_BYTES)

    def test_confirm_saves_entry_pdf_hash_and_change_log(self):
        with patch("paperlab_local.paper_import._llm_fields", return_value={}):
            preview = preview_pdf(self.db, sample_pdf())
        result = finalize_import(self.db, self.root, {
            "import_token": preview["import_token"],
            "fields": {"title": "Imported Systems Paper", "keywords": "cache, serving"},
        })
        self.assertTrue(result["created"])
        with Store(self.db, backup_on_migrate=False) as store:
            paper = store.get(result["paper_id"])
            self.assertEqual(paper["keywords"], "cache, serving")
            self.assertTrue(Path(paper["pdf_path"]).is_file())
            self.assertEqual(store.conn.execute("SELECT count(*) FROM stored_objects").fetchone()[0], 1)
            self.assertGreaterEqual(store.conn.execute("SELECT count(*) FROM sync_changes").fetchone()[0], 2)

    def test_duplicate_requires_confirmation_then_merges(self):
        with Store(self.db, backup_on_migrate=False) as store:
            paper_id = store.upsert_candidate({"title": "Same Paper", "abstract": "Existing"})
        conflict = finalize_import(self.db, self.root, {
            "fields": {"title": "Same Paper", "abstract": "Incoming", "organizations": "MIT"},
        })
        self.assertEqual(conflict["conflict"]["id"], paper_id)
        merged = finalize_import(self.db, self.root, {
            "fields": {"title": "Same Paper", "abstract": "Incoming", "organizations": "MIT"},
            "duplicate_action": "fill_missing",
        })
        self.assertTrue(merged["merged"])
        with Store(self.db, backup_on_migrate=False) as store:
            self.assertEqual(store.get(paper_id)["abstract"], "Existing")
            self.assertEqual(store.get(paper_id)["organizations"], "MIT")

    def test_expired_preview_is_removed(self):
        with patch("paperlab_local.paper_import._llm_fields", return_value={}):
            preview = preview_pdf(self.db, sample_pdf())
        path = self.db.parent / "import-temp" / (preview["import_token"] + ".pdf")
        old = time.time() - 1900
        os.utime(path, (old, old))
        cleanup_previews(self.db)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
