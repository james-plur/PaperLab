import json
import os
import tempfile
import unittest
from pathlib import Path

from paperlab import index as index_mod
from paperlab.store import Store


class TestIndex(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.mkdtemp()
        self.store = Store(os.path.join(self._d, "t.db"))
        # one fully-extracted paper with a pdf figure, one mermaid, one bare
        fig = os.path.join(self._d, "f.png")
        Path(fig).write_bytes(b"\x89PNG\r\n\x1a\n")
        self.store.upsert_candidate(
            {"title": "Alpha", "arxiv_id": "2401.00001",
             "abstract": "ab", "venue": "OSDI", "year": 2024}, "llm")
        pid = "arxiv:2401.00001"
        self.store.set_relevance(pid, 0.9)
        self.store.set_pdf(pid, path=os.path.join(self._d, "a.pdf"), status="ok")
        self.store.set_extraction(pid, target_problem="slow inference",
            core_idea="batching", organizations="MIT", github_url="github.com/a/b",
            figure_kind="pdf", figure_path=fig)

        self.store.upsert_candidate(
            {"title": "Beta", "venue": "SOSP", "year": 2023}, "llm")
        self.store.set_relevance("thash:" + _hash("Beta"), 0.7)
        self.store.set_extraction("thash:" + _hash("Beta"),
            target_problem="x", core_idea="y", organizations="Stanford",
            github_url=None, figure_kind="mermaid", figure_mermaid="graph TD\n  A-->B")

    def tearDown(self):
        self.store.close()

    def test_build_site(self):
        site = Path(self._d) / "site"
        out = index_mod.build(self.store, ["llm"], site_dir=site)
        self.assertTrue(out.exists())
        html = out.read_text(encoding="utf-8")
        self.assertIn("PaperLab", html)
        self.assertIn("Alpha", html)
        self.assertIn("github.com/a/b", html)
        data = json.loads((site / "data.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data), 2)
        kinds = {d["figure_kind"] for d in data}
        self.assertEqual(kinds, {"pdf", "mermaid"})
        # relative figure path points back to the data dir
        pdf_row = next(d for d in data if d["figure_kind"] == "pdf")
        self.assertTrue(pdf_row["figure_rel"].endswith("f.png"))
        # papers.jsonl has one line per paper
        lines = (site / "papers.jsonl").read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 2)


def _hash(title):
    import hashlib, re
    t = re.sub(r"[^\w\s]", " ", title.lower())
    t = re.sub(r"\s+", " ", t).strip()
    return hashlib.sha1(t.encode()).hexdigest()[:12]


if __name__ == "__main__":
    unittest.main()
