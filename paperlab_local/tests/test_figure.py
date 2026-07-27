import os
import tempfile
import unittest
from pathlib import Path

from paperlab_local import figure as figure_mod
from paperlab_local import pdf as pdf_mod


def _write_pdf(path, blocks):
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    for kind, arg in blocks:
        if kind == "rect":
            page.draw_rect(pymupdf.Rect(*arg), color=(0, 0, 0), width=1)
        elif kind == "text":
            page.insert_text((arg[0], arg[1]), arg[2], fontsize=10)
    doc.save(path)
    doc.close()


class TestFigure(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.mkdtemp()

    def test_caption_match_crops_figure(self):
        pdf = os.path.join(self._d, "a.pdf")
        _write_pdf(pdf, [
            ("text", (60, 80, "Some intro paragraph text here.")),
            ("rect", (50, 110, 350, 260)),
            ("text", (60, 300, "Figure 1: Overview of the system architecture."),
            ),
        ])
        out = os.path.join(self._d, "fig.png")
        ok = figure_mod.detect_from_pdf(pdf, out)
        self.assertTrue(ok)
        self.assertTrue(Path(out).exists())
        self.assertGreater(Path(out).stat().st_size, 0)

    def test_no_caption_returns_false(self):
        pdf = os.path.join(self._d, "b.pdf")
        _write_pdf(pdf, [("text", (60, 80, "Hello world, nothing to see here."))])
        out = os.path.join(self._d, "none.png")
        self.assertFalse(figure_mod.detect_from_pdf(pdf, out))
        self.assertFalse(Path(out).exists())


class TestGithub(unittest.TestCase):
    def test_finds_repo_root(self):
        d = tempfile.mkdtemp()
        pdf = os.path.join(d, "g.pdf")
        _write_pdf(pdf, [
            ("text", (60, 80, "Code at github.com/foo/bar/tree/main/src see also")),
        ])
        self.assertEqual(pdf_mod.find_github_url(pdf), "github.com/foo/bar")

    def test_none_when_absent(self):
        d = tempfile.mkdtemp()
        pdf = os.path.join(d, "n.pdf")
        _write_pdf(pdf, [("text", (60, 80, "No repository mentioned at all."))])
        self.assertIsNone(pdf_mod.find_github_url(pdf))


if __name__ == "__main__":
    unittest.main()
