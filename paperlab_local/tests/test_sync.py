import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from paperlab_local import sync
from paperlab_local.store import Store


class _MissingKey(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    class exceptions:
        NoSuchKey = _MissingKey

    def __init__(self):
        self.objects = {}
        self.downloads = []

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        self.objects[Key] = bytes(Body)

    def get_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise _MissingKey()
        return {"Body": io.BytesIO(self.objects[Key])}

    def upload_file(self, filename, bucket, key):
        self.objects[key] = Path(filename).read_bytes()

    def download_file(self, bucket, key, filename):
        self.downloads.append(key)
        Path(filename).write_bytes(self.objects[key])

    def list_objects_v2(self, *, Bucket, Prefix, **kwargs):
        rows = [{"Key": key} for key in sorted(self.objects) if key.startswith(Prefix)]
        return {"Contents": rows, "IsTruncated": False}

    def delete_objects(self, *, Bucket, Delete):
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)


def _copy(source, destination):
    shutil.copyfile(source, destination)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "data/papers.db"
        self.cfg = SimpleNamespace(root=self.root, db_path=self.db)
        self.client = FakeS3()
        self.env = patch.dict("os.environ", {
            "PAPERLAB_SYNC_BUCKET": "bucket",
            "PAPERLAB_SYNC_REGION": "auto",
        }, clear=False)
        self.client_patch = patch("paperlab_local.sync._client", return_value=self.client)
        self.compress_patch = patch("paperlab_local.sync._compress", side_effect=_copy)
        self.decompress_patch = patch("paperlab_local.sync._decompress", side_effect=_copy)
        self.env.start()
        self.client_patch.start()
        self.compress_patch.start()
        self.decompress_patch.start()

    def tearDown(self):
        self.decompress_patch.stop()
        self.compress_patch.stop()
        self.client_patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def _seed(self):
        report = self.root / "reports/test.md"
        report.parent.mkdir(parents=True)
        report.write_text("# report", encoding="utf-8")
        pdf = self.root / "pdfs/test.pdf"
        pdf.parent.mkdir(parents=True)
        pdf.write_bytes(b"pdf")
        with Store(self.db, backup_on_migrate=False) as store:
            paper_id = store.upsert_candidate({"title": "Synced Paper", "year": 2025})
            store.set_pdf(paper_id, path=str(pdf), status="ok")
            store.set_report(paper_id, report_path=str(report))

    def test_push_and_pull_without_pdfs(self):
        self._seed()
        pushed = sync.push(self.cfg, log=lambda *_: None)
        self.assertEqual(pushed["file_uploads"], 2)
        manifest = json.loads(self.client.objects["manifests/latest.json"])
        self.assertEqual(manifest["parent_revision"], None)

        self.db.unlink()
        (self.root / "reports/test.md").unlink()
        (self.root / "pdfs/test.pdf").unlink()
        pulled = sync.pull(self.cfg, include_pdfs=False, log=lambda *_: None)
        self.assertTrue(self.db.is_file())
        self.assertTrue((self.root / "reports/test.md").is_file())
        self.assertFalse((self.root / "pdfs/test.pdf").exists())
        self.assertEqual(pulled["downloaded_files"], 1)
        with Store(self.db, backup_on_migrate=False) as store:
            paper = store.conn.execute("SELECT pdf_path FROM papers").fetchone()
            report = store.conn.execute("SELECT report_path,status FROM paper_reports").fetchone()
            self.assertIsNone(paper["pdf_path"])
            self.assertEqual(report["report_path"], str((self.root / "reports/test.md").resolve()))
            self.assertEqual(report["status"], "ok")

        downloads = list(self.client.downloads)
        sync.pull(self.cfg, include_pdfs=False, log=lambda *_: None)
        self.assertEqual(self.client.downloads, downloads)
        self.assertEqual(list((self.root / "data").glob("*.bak-sync-*")), [])

    def test_push_rejects_unrelated_remote_revision(self):
        self._seed()
        self.client.objects["manifests/latest.json"] = json.dumps({
            "revision": "other", "database": {}, "files": {}
        }).encode()
        with self.assertRaisesRegex(RuntimeError, "remote data changed"):
            sync.push(self.cfg, log=lambda *_: None)

    def test_gc_is_dry_run_by_default(self):
        self._seed()
        sync.push(self.cfg, log=lambda *_: None)
        self.client.objects["pdfs/orphan.pdf"] = b"old"
        result = sync.garbage_collect(self.cfg, log=lambda *_: None)
        self.assertEqual(result["unreferenced_objects"], 1)
        self.assertIn("pdfs/orphan.pdf", self.client.objects)

    def test_real_zstandard_round_trip(self):
        self.compress_patch.stop()
        self.decompress_patch.stop()
        try:
            source = self.root / "source.bin"
            compressed = self.root / "source.zst"
            restored = self.root / "restored.bin"
            source.write_bytes((b"paperlab_local-data-" * 10000) + b"end")
            sync._compress(source, compressed)
            sync._decompress(compressed, restored)
            self.assertEqual(source.read_bytes(), restored.read_bytes())
            self.assertLess(compressed.stat().st_size, source.stat().st_size)
        finally:
            self.compress_patch.start()
            self.decompress_patch.start()


if __name__ == "__main__":
    unittest.main()
