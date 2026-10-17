import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from paperlab_local.store import Store
from paperlab_local.incremental import apply_change, push_to_server


class SyncRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = SimpleNamespace(root=self.root, pdf_root=self.root)
        self.store = Store(self.root / "test.db", backup_on_migrate=False)
        self.paper = self.store.upsert_candidate({"title": "Concurrent PDF"})
        self.pdf = self.root / "paper.pdf"
        self.pdf.write_bytes(b"pdf fixture")
        self.store.set_pdf(self.paper, path=str(self.pdf), status="ok")

    def tearDown(self):
        self.store.conn.close()
        self.temp.cleanup()

    def change(self, **fields):
        return dict(entity_type="paper", entity_id=self.paper, operation="update",
                    base_version=2, version=3, changed_fields=fields)

    def test_remove_pdf_clears_reference(self):
        apply_change(self.store, self.change(pdf_status="missing", object_sha256=None), self.cfg)
        row = self.store.get(self.paper)
        self.assertIsNone(row["pdf_path"])
        self.assertEqual(row["pdf_status"], "missing")

    def test_concurrent_pdf_creates_conflict_without_overwrite(self):
        change = self.change(pdf_status="ok", object_sha256="a" * 64)
        change["base_version"] = 1
        conflicts = apply_change(self.store, change, self.cfg)
        self.assertIn("object_sha256", conflicts)
        self.assertEqual(self.store.get(self.paper)["pdf_path"], str(self.pdf))

    def test_unknown_entity_is_rejected_without_journaling(self):
        change = dict(change_id="unknown-test", entity_type="future_type", entity_id="x",
                      operation="update", changed_fields={})
        result = push_to_server(self.store, [change], self.cfg)
        self.assertFalse(result["accepted"])
        self.assertEqual(len(result["rejected"]), 1)
        self.assertIsNone(self.store.conn.execute(
            "SELECT 1 FROM sync_changes WHERE change_id='unknown-test'").fetchone())

    def test_unknown_pull_change_fails(self):
        change = self.change()
        change["entity_type"] = "future_type"
        with self.assertRaises(ValueError):
            apply_change(self.store, change, self.cfg)

    def test_stale_note_save_preserves_file_and_version(self):
        note = self.store.save_note_content(self.root, self.paper, "new content", base_version=0)
        with self.assertRaises(RuntimeError):
            self.store.save_note_content(self.root, self.paper, "stale content", base_version=0)
        self.assertEqual(Path(note["note_path"]).read_text(), "new content")
        self.assertEqual(self.store.get_note(self.paper)["version"], 1)

    def test_failed_note_database_update_restores_file(self):
        from unittest.mock import patch
        note = self.store.save_note_content(self.root, self.paper, "original", base_version=0)
        with patch.object(self.store, "set_note", side_effect=RuntimeError("write failed")):
            with self.assertRaises(RuntimeError):
                self.store.save_note_content(self.root, self.paper, "replacement", base_version=1)
        self.assertEqual(Path(note["note_path"]).read_text(), "original")
        self.assertEqual(self.store.get_note(self.paper)["version"], 1)

    def test_equal_overlapping_paper_fields_do_not_conflict(self):
        paper = self.store.get(self.paper)
        change = self.change(title=paper["title"], pdf_status=paper["pdf_status"])
        change["base_version"] = 0
        self.assertEqual(apply_change(self.store, change, self.cfg), [])
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM sync_conflicts").fetchone()[0], 0)

    def test_equal_overlapping_pdf_object_does_not_conflict(self):
        digest = self.store.conn.execute("SELECT sha256 FROM stored_objects").fetchone()[0]
        change = self.change(object_sha256=digest)
        change["base_version"] = 0
        self.assertEqual(apply_change(self.store, change, self.cfg), [])

    def test_incoming_change_retains_source_timestamp(self):
        change = self.change(title="Timestamp survives synchronization")
        change.update(change_id="timestamp-test", created_at=1700000000.25)
        result = push_to_server(self.store, [change], self.cfg)
        self.assertEqual(len(result["accepted"]), 1)
        row = self.store.conn.execute("SELECT created_at,applied_at FROM sync_changes WHERE change_id='timestamp-test'").fetchone()
        self.assertEqual(row["created_at"], 1700000000.25)
        self.assertGreater(row["applied_at"], row["created_at"])
