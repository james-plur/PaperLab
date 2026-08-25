import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from paperlab_local.reader import ask_pdf, extract_pdf_text, load_note, save_note


class ReaderTests(unittest.TestCase):
    def test_notes_are_persisted_outside_the_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = save_note(root, "doi:10.1/example", "# Notes\nUseful result")
            self.assertTrue(path.is_relative_to(root / "reading-notes"))
            self.assertEqual(load_note(root, "doi:10.1/example"), "# Notes\nUseful result")

    def test_pdf_text_keeps_page_markers_for_grounded_answers(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "paper.pdf"
            document = pymupdf.open()
            document.new_page().insert_text((72, 72), "First page claim")
            document.new_page().insert_text((72, 72), "Second page result")
            document.save(path)
            document.close()
            text = extract_pdf_text(str(path))
            self.assertIn("--- Page 1 ---", text)
            self.assertIn("First page claim", text)
            self.assertIn("--- Page 2 ---", text)

    def test_qa_uses_ordered_provider_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "paper.pdf"
            document = pymupdf.open()
            document.new_page().insert_text((72, 72), "A grounded result")
            document.save(path)
            document.close()
            with patch("paperlab_local.llm.chat_text", side_effect=[RuntimeError("quota"), "Fallback answer"]) as chat:
                result = ask_pdf(Path(temp) / "papers.db", {
                    "title": "Paper", "authors": "Author", "pdf_path": str(path),
                }, "What is the result?")
            self.assertEqual(result["answer"], "Fallback answer")
            self.assertEqual(result["model"], "GLM-4.7-Flash")
            self.assertTrue(result["fallback"])
            self.assertEqual(chat.call_args_list[0].kwargs["model"], "deepseek-v4-flash")
