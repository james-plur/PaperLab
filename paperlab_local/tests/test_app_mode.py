import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import pymupdf
import requests

from paperlab_local.store import Store
from paperlab_local.web import PaperHandler


class AppModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = root / "papers.db"
        pdf = root / "paper.pdf"
        doc = pymupdf.open()
        for index in range(5):
            page = doc.new_page(); page.insert_text((72, 72), "PaperLab desktop PDF " + str(index))
        doc.save(pdf); doc.close()
        with Store(self.db) as store:
            self.paper_id = store.upsert_candidate({"title": "Desktop Paper", "year": 2026})
            store.set_pdf(self.paper_id, path=str(pdf), status="ok")
        handler = type("AppHandler", (PaperHandler,), {
            "db_path": self.db, "config_path": root / "topics.yaml",
            "app_token": "app-secret", "no_browser_ui": True,
        })
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.headers = {"Authorization": "Bearer app-secret"}

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.temp.cleanup()

    def test_health_is_public_but_data_requires_token(self):
        self.assertEqual(requests.get(self.url + "/api/app/health").status_code, 200)
        self.assertEqual(requests.get(self.url + "/api/papers").status_code, 401)
        papers = requests.get(self.url + "/api/papers", headers=self.headers).json()
        self.assertEqual(papers["total"], 1)

    def test_detail_and_pdf_range(self):
        detail = requests.get(self.url + "/api/papers/" + self.paper_id, headers=self.headers)
        self.assertEqual(detail.json()["title"], "Desktop Paper")
        response = requests.get(self.url + "/pdf/" + self.paper_id,
                                headers={**self.headers, "Range": "bytes=0-15"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(len(response.content), 16)
        self.assertTrue(response.headers["Content-Range"].startswith("bytes 0-15/"))

    def test_browser_ui_is_disabled(self):
        self.assertEqual(requests.get(self.url + "/", headers=self.headers).status_code, 404)


if __name__ == "__main__":
    unittest.main()
