import unittest
from unittest import mock

from paperlab.venues import systems


class TestParsers(unittest.TestCase):
    def test_usenix(self):
        raw = """<html><body>
        <h3>Session 1</h3>
        Some Nice Paper Title Here
        Jane Doe (MIT), Ralph Roe (Stanford)
        Available Media
        A Keynote By Someone At Google
        Available Media
        </body></html>"""
        out = systems.parse_usenix(raw)
        self.assertEqual(len(out), 1)
        self.assertIn("Nice Paper", out[0]["title"])
        self.assertIn("Jane Doe", out[0]["authors"])

    def test_usenix_details_includes_award_abstract_and_orgs(self):
        raw = '''<article id="node-1" class="node node-paper view-mode-schedule">
        <h2><a href="/conference/fast25/presentation/qin">Mooncake: KV Cache</a></h2>
        <div class="field-name-field-paper-people-text"><div class="field-item odd"><p>
        Alice, <em>Moonshot AI;</em> Bob, <em>Tsinghua University</em></p>
        <p>Awarded Best Paper!</p></div></div>
        <div class="field-name-field-paper-description-long"><div class="field-items field-items">
        <p>A disaggregated KV cache architecture.</p>
        <div class="field field-name-field-paper-people"></div></div></div></article>'''
        paper = systems.parse_usenix_details(raw)[0]
        self.assertEqual(paper["awards"], "Awarded Best Paper!")
        self.assertIn("Moonshot AI", paper["organizations"])
        self.assertIn("disaggregated", paper["abstract"])
        self.assertTrue(paper["source_url"].endswith("/presentation/qin"))

    def test_infocom(self):
        raw = """1. First Paper Title
        Alice, Bob
        2. Second Paper Title
        Carol, Dave"""
        out = systems.parse_infocom(raw)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["title"], "First Paper Title")

    def test_isca(self):
        raw = """<p>1:30 - 2:00 PM</p>
        <p>An Architecture for Things</p>
        <p>Author One (MIT), Author Two (Berkeley)</p>
        <p>2:00 - 2:30 PM</p>
        <p>Welcome Reception</p>
        <p>Host</p>"""
        out = systems.parse_isca(raw)
        self.assertEqual(len(out), 1)
        self.assertIn("Architecture", out[0]["title"])

    def test_dblp(self):
        raw = """<ul>
        <li class="entry inproceedings">
        <span class="title" itemprop="name">Vector Search At Scale</span>
        <span itemprop="author"><span itemprop="name">Q. Liu</span></span>
        <span itemprop="author"><span itemprop="name">R. Zhao</span></span>
        </li></ul>"""
        out = systems.parse_dblp(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "Vector Search At Scale")
        self.assertEqual(out[0]["authors"], "Q. Liu, R. Zhao")

    def test_hpca(self):
        raw = """<div>
        <strong><a data-event-modal="x">A Fast Cache Design</a></strong>
        <div class="prog-track">Main Conference</div>
        <div class="performers">N. Wu (Tsinghua)
        <span class="prog-aff">Lab</span></div>
        <strong><a data-event-modal="y">Coffee break</a></strong>
        <div class="performers">-</div>
        </div>"""
        out = systems.parse_hpca(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "A Fast Cache Design")
        self.assertEqual(out[0]["track"], "Main Conference")

    def test_sigcomm(self):
        raw = """The following papers accepted.
        <b>Flow Scheduling At Scale</b> Alice (MIT), Bob (Stanford)
        <b>Sponsors</b> Acme Corp"""
        out = systems.parse_sigcomm(raw)
        self.assertEqual(len(out), 1)
        self.assertIn("Flow Scheduling", out[0]["title"])

    def test_sosp(self):
        raw = """Accepted Papers
        A Distributed OS for the Ages
        Author One (MIT), Author Two (Berkeley)
        Welcome
        Host"""
        out = systems.parse_sosp(raw)
        self.assertEqual(len(out), 1)
        self.assertIn("Distributed OS", out[0]["title"])

    def test_fetch_preserves_dblp_metadata(self):
        candidate = {"title": "A Paper", "authors": "A", "doi": "10.1/x",
                     "source_url": "https://doi.org/10.1/x", "source": "dblp"}
        with mock.patch.object(systems, "_fetch_dblp_year", return_value=[candidate]):
            out = systems.fetch("ISCA", 2024)
        self.assertEqual(out[0]["doi"], "10.1/x")
        self.assertEqual(out[0]["venue"], "ISCA")


if __name__ == "__main__":
    unittest.main()
