import json
import hashlib
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
import pymupdf

from paperlab_publish.auth import AUTH, hash_password
from paperlab_publish.auth import AuthManager
from paperlab_publish.incremental import change_dict, push_to_server, sync_now
from paperlab_publish.store import Store
from paperlab_publish.web import PaperHandler, update_paper


def _upload_pdf():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "HTTP Imported Paper", fontsize=20)
    page.insert_text((72, 120), "Abstract: A manually imported systems paper.\n1 Introduction", fontsize=11)
    doc.set_metadata({"title": "HTTP Imported Paper", "author": "Alice Example"})
    value = doc.tobytes()
    doc.close()
    return value


class AuthWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "data/papers.db"
        self.config = self.root / "topics.yaml"
        self.config.write_text("pdf_root: .\ndb_path: data/papers.db\n", encoding="utf-8")
        with Store(self.db, backup_on_migrate=False) as store:
            store.upsert_candidate({"title": "Public Paper", "abstract": "Readable"})
        self.env = patch.dict(os.environ, {
            "PAPERLAB_LOCAL_TRUSTED_MODE": "0",
            "PAPERLAB_COOKIE_SECURE": "0",
            "PAPERLAB_ADMIN_PASSWORD_HASH": hash_password("correct horse battery staple"),
        }, clear=False)
        self.env.start()
        AUTH._sessions.clear()
        AUTH._failures.clear()
        handler = type("TestHandler", (PaperHandler,), {
            "db_path": self.db, "config_path": self.config,
        })
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.env.stop()
        self.temp.cleanup()

    def test_anonymous_read_login_csrf_and_edit(self):
        public = requests.get(self.url + "/api/papers", timeout=5)
        self.assertEqual(public.status_code, 200)
        paper = public.json()["papers"][0]
        self.assertNotIn(str(self.root), json.dumps(paper))
        self.assertIn("noindex", public.headers["X-Robots-Tag"])

        denied = requests.post(self.url + "/api/edit", json={"paper_id": paper["id"]}, timeout=5)
        self.assertEqual(denied.status_code, 401)
        llm = requests.get(self.url + "/api/llm/config", timeout=5)
        self.assertEqual(llm.status_code, 401)

        session = requests.Session()
        login = session.post(self.url + "/api/auth/login",
                             json={"username": "admin", "password": "correct horse battery staple"}, timeout=5)
        self.assertEqual(login.status_code, 200)
        csrf = login.json()["csrf_token"]
        no_csrf = session.post(self.url + "/api/edit", json={"paper_id": paper["id"]}, timeout=5)
        self.assertEqual(no_csrf.status_code, 403)
        edited = session.post(
            self.url + "/api/edit", headers={"X-CSRF-Token": csrf}, timeout=5,
            json={"paper_id": paper["id"], "base_version": paper["version"],
                  "fields": {"title": "Edited"}},
        )
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.json()["paper"]["title"], "Edited")

    def test_device_object_upload_is_resumable_and_hash_checked(self):
        token = "device-secret"
        with Store(self.db, backup_on_migrate=False) as store:
            store.register_device("mac", "Mac", AuthManager.token_hash(token))
        headers = {"Authorization": "Bearer " + token}
        content = b"a" * 100 + b"b" * 100
        digest = hashlib.sha256(content).hexdigest()
        url = self.url + "/api/sync/objects/" + digest
        first = requests.put(url, headers={**headers, "X-Upload-Offset": "0",
                                           "X-Upload-Total": str(len(content)),
                                           "Content-Type": "application/pdf"},
                             data=content[:100], timeout=5)
        self.assertEqual(first.status_code, 200)
        partial = requests.head(url, headers=headers, timeout=5)
        self.assertEqual(partial.status_code, 404)
        self.assertEqual(partial.headers["X-Upload-Offset"], "100")
        second = requests.put(url, headers={**headers, "X-Upload-Offset": "100",
                                            "X-Upload-Total": str(len(content)),
                                            "Content-Type": "application/pdf"},
                              data=content[100:], timeout=5)
        self.assertTrue(second.json()["complete"])
        downloaded = requests.get(url, headers=headers, timeout=5)
        self.assertEqual(downloaded.content, content)

    def test_admin_can_rename_topic_and_edit_markdown_with_versions(self):
        session = requests.Session()
        login = session.post(self.url + "/api/auth/login",
                             json={"username": "admin", "password": "correct horse battery staple"}, timeout=5)
        csrf = login.json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf, "Content-Type": "application/json"}
        topic = session.post(self.url + "/api/topics", headers=headers,
                             json={"name": "Old name"}, timeout=5).json()
        renamed = session.patch(self.url + f"/api/topics/{topic['id']}", headers=headers,
                                json={"name": "New name", "base_version": topic["version"]}, timeout=5)
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["name"], "New name")
        paper_id = requests.get(self.url + "/api/papers", timeout=5).json()["papers"][0]["id"]
        saved = session.put(self.url + "/api/reports/" + paper_id, headers=headers,
                            json={"content": "# Safe\n<script>alert(1)</script>", "base_version": 0}, timeout=5)
        self.assertEqual(saved.status_code, 200)
        report = requests.get(self.url + "/api/report", params={"id": paper_id}, timeout=5).json()
        self.assertEqual(report["content"], "# Safe\n<script>alert(1)</script>")
        history = session.get(self.url + "/api/history/report/" + paper_id, timeout=5).json()
        self.assertEqual(history["versions"][0]["version"], 1)
        self.assertIn("let h=esc(md)", __import__("paperlab_publish.web", fromlist=["HTML"]).HTML)

    def test_add_paper_preview_manual_create_and_duplicate_merge(self):
        denied = requests.post(self.url + "/api/papers/import/preview",
                               headers={"Content-Type": "application/pdf"}, data=_upload_pdf(), timeout=5)
        self.assertEqual(denied.status_code, 401)
        session = requests.Session()
        login = session.post(self.url + "/api/auth/login",
                             json={"username": "admin", "password": "correct horse battery staple"}, timeout=5)
        csrf = login.json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf, "Content-Type": "application/pdf"}
        no_csrf = session.post(self.url + "/api/papers/import/preview",
                               headers={"Content-Type": "application/pdf"}, data=_upload_pdf(), timeout=5)
        self.assertEqual(no_csrf.status_code, 403)
        invalid = session.post(self.url + "/api/papers/import/preview", headers=headers,
                               data=b"not-pdf", timeout=5)
        self.assertEqual(invalid.status_code, 400)
        with patch("paperlab_publish.paper_import._llm_fields", return_value={"keywords": "systems, import"}):
            preview = session.post(self.url + "/api/papers/import/preview", headers=headers,
                                   data=_upload_pdf(), timeout=5)
        self.assertEqual(preview.status_code, 200)
        token = preview.json()["import_token"]
        json_headers = {"X-CSRF-Token": csrf, "Content-Type": "application/json"}
        created = session.post(self.url + "/api/papers", headers=json_headers, json={
            "import_token": token,
            "fields": {"title": "HTTP Imported Paper", "abstract": "Original", "keywords": "systems"},
        }, timeout=5)
        self.assertEqual(created.status_code, 201)
        self.assertTrue(created.json()["has_pdf"])
        duplicate = session.post(self.url + "/api/papers", headers=json_headers,
                                 json={"fields": {"title": "HTTP Imported Paper", "abstract": "Changed"}}, timeout=5)
        self.assertEqual(duplicate.status_code, 409)
        merged = session.post(self.url + "/api/papers", headers=json_headers, json={
            "fields": {"title": "HTTP Imported Paper", "abstract": "Changed", "organizations": "MIT"},
            "duplicate_action": "fill_missing",
        }, timeout=5)
        self.assertEqual(merged.status_code, 200)
        with Store(self.db, backup_on_migrate=False) as store:
            row = store.get(created.json()["paper_id"])
            self.assertEqual(row["abstract"], "Original")
            self.assertEqual(row["organizations"], "MIT")

    def test_http_bidirectional_incremental_sync(self):
        token = "sync-secret"
        with Store(self.db, backup_on_migrate=False) as server:
            server.register_device("mac", "Mac", AuthManager.token_hash(token))
            server.conn.execute("DELETE FROM sync_changes")
            server.conn.commit()
            pid = server.conn.execute("SELECT id FROM papers").fetchone()[0]
        local_root = self.root / "local"
        local_db = local_root / "data/papers.db"
        with Store(local_db, backup_on_migrate=False) as local:
            local.upsert_candidate({"title": "Public Paper", "abstract": "Readable"})
            local.conn.execute("DELETE FROM sync_changes")
            local.conn.commit()
        update_paper(local_db, pid, {"title": "From local"}, base_version=1)
        cfg = SimpleNamespace(root=local_root, db_path=local_db, pdf_root=local_root)
        sync_env = patch.dict(os.environ, {
            "PAPERLAB_SYNC_URL": self.url,
            "PAPERLAB_DEVICE_TOKEN": token,
            "PAPERLAB_DEVICE_ID": "mac",
            "PAPERLAB_SYNC_SERVER": "0",
        }, clear=False)
        with sync_env:
            first = sync_now(cfg, log=lambda *_: None)
            self.assertEqual(first["pushed"], 1)
            with Store(self.db, backup_on_migrate=False) as server:
                self.assertEqual(server.get(pid)["title"], "From local")
            with patch.dict(os.environ, {"PAPERLAB_SYNC_SERVER": "1", "PAPERLAB_DEVICE_ID": "server"}):
                update_paper(self.db, pid, {"abstract": "From cloud"}, base_version=2)
            second = sync_now(cfg, log=lambda *_: None)
            self.assertGreaterEqual(second["pulled"], 1)
        with Store(local_db, backup_on_migrate=False) as local:
            self.assertEqual(local.get(pid)["abstract"], "From cloud")


class IncrementalTests(unittest.TestCase):
    def test_push_is_idempotent_and_conflicts_keep_both_values(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            local_db = root / "local.db"
            server_db = root / "server.db"
            with Store(local_db, backup_on_migrate=False) as local, \
                 Store(server_db, backup_on_migrate=False) as server:
                pid = local.upsert_candidate({"title": "Original"})
                server.upsert_candidate({"title": "Original"})
                local.conn.execute("UPDATE sync_changes SET uploaded_at=?", (1,))
                server.conn.execute("UPDATE sync_changes SET uploaded_at=?,server_sequence=1", (1,))
                local.conn.commit(); server.conn.commit()

                update_paper(local_db, pid, {"title": "Local title"}, base_version=1)
                pending = [change_dict(row) for row in local.pending_changes()]
                cfg = type("Cfg", (), {"root": root, "pdf_root": root})()
                first = push_to_server(server, pending, cfg)
                second = push_to_server(server, pending, cfg)
                self.assertEqual(first["accepted"][0]["server_sequence"],
                                 second["accepted"][0]["server_sequence"])
                self.assertEqual(server.get(pid)["title"], "Local title")

                update_paper(local_db, pid, {"abstract": "local abstract"}, base_version=2)
                update_paper(server_db, pid, {"abstract": "server abstract"}, base_version=2)
                conflict_change = change_dict(local.pending_changes()[-1])
                result = push_to_server(server, [conflict_change], cfg)
                self.assertIn("abstract", result["accepted"][0]["conflicts"])
                conflict = server.conn.execute(
                    "SELECT * FROM sync_conflicts WHERE status='open'"
                ).fetchone()
                self.assertEqual(json.loads(conflict["local_value"]), "server abstract")
                self.assertEqual(json.loads(conflict["remote_value"]), "local abstract")


if __name__ == "__main__":
    unittest.main()
