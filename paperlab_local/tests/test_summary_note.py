import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from paperlab_local.incremental import apply_change
from paperlab_local.reader import _note_path, reconcile_notes
from paperlab_local.store import Store
from paperlab_local.summary import generate_summary, public_summary


class SummaryAndNoteTests(unittest.TestCase):
    def test_summary_uses_abstract_without_pdf_and_becomes_stale(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "papers.db"
            with Store(db, backup_on_migrate=False) as store:
                paper_id = store.upsert_candidate({"title": "Abstract only", "abstract": "A new method."})
                with patch("paperlab_local.llm.chat_text_fallback", return_value={
                    "answer": "这是一段中文概要。", "provider": "siliconflow", "model": "m"
                }):
                    result = generate_summary(store, paper_id)
                self.assertEqual(result["content"], "这是一段中文概要。")
                self.assertFalse(public_summary(store, paper_id)["stale"])
                store.conn.execute("UPDATE papers SET abstract='Changed' WHERE id=?", (paper_id,)); store.conn.commit()
                self.assertTrue(public_summary(store, paper_id)["stale"])

    def test_note_versions_sync_and_conflicts_preserve_both_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); db = root / "papers.db"; cfg = SimpleNamespace(pdf_root=root)
            with Store(db, backup_on_migrate=False) as store:
                paper_id = store.upsert_candidate({"title": "Notes"})
                path = _note_path(root, paper_id); path.parent.mkdir(); path.write_text("local", encoding="utf-8")
                store.set_note(paper_id, note_path=str(path), content="local")
                conflicts = apply_change(store, {
                    "entity_type":"note", "entity_id":paper_id, "operation":"update",
                    "base_version":0, "version":2, "device_id":"other mac",
                    "changed_fields":{"content":"remote", "content_hash":"remote-hash"},
                }, cfg)
                self.assertEqual(conflicts, ["content"])
                note = store.get_note(paper_id)
                self.assertEqual(Path(note["note_path"]).read_text(encoding="utf-8"), "remote")
                copies = list((root / "reading-notes").glob("*.conflict-other_mac-*.md"))
                self.assertEqual(len(copies), 1)
                self.assertEqual(copies[0].read_text(encoding="utf-8"), "local")

    def test_existing_markdown_note_is_registered_for_sync(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); db=root/"papers.db"
            with Store(db,backup_on_migrate=False) as store:
                paper_id=store.upsert_candidate({"title":"Existing note"})
                path=_note_path(root,paper_id); path.parent.mkdir(); path.write_text("legacy note",encoding="utf-8")
                self.assertEqual(reconcile_notes(store,root),1)
                self.assertEqual(store.get_note(paper_id)["version"],1)
                self.assertEqual(store.conn.execute("SELECT count(*) FROM sync_changes WHERE entity_type='note'").fetchone()[0],1)

    def test_v11_migration_deletes_legacy_report_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); db = root / "papers.db"; legacy = root / "legacy.md"
            legacy.write_text("old report", encoding="utf-8")
            with Store(db, backup_on_migrate=False) as store:
                paper_id = store.upsert_candidate({"title":"Legacy"})
                store.set_report(paper_id, report_path=str(legacy))
                store.conn.execute("PRAGMA user_version=10"); store.conn.commit()
            with Store(db, backup_on_migrate=False) as migrated:
                self.assertFalse(legacy.exists())
                self.assertEqual(migrated.conn.execute("SELECT count(*) FROM paper_reports").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
