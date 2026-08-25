import tempfile
import unittest
from pathlib import Path

from paperlab_local.app_config import read_settings, restore_backup, save_settings
from paperlab_local.store import Store


class AppUpgradeTests(unittest.TestCase):
    def test_activity_is_local_deduplicated_and_joined_to_paper(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "papers.db"
            with Store(db) as store:
                paper_id = store.upsert_candidate({"title": "Recent Paper", "authors": "Ada"})
                store.record_activity(paper_id, "viewed", dedupe_seconds=300)
                store.record_activity(paper_id, "viewed", dedupe_seconds=300)
                store.record_activity(paper_id, "reading_started")
                rows = store.recent_activity(limit=10, unique_papers=True)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["title"], "Recent Paper")
                self.assertEqual(rows[0]["action"], "reading_started")
                self.assertEqual(store.conn.execute("SELECT count(*) FROM paper_activity").fetchone()[0], 2)

    def test_yaml_round_trip_preserves_comments_unknown_keys_and_backups(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "topics.yaml"
            path.write_text("# keep me\npdf_root: .\ndb_path: data/papers.db\ncustom: yes\n", encoding="utf-8")
            result = save_settings(path, {"data": {"schedule": {"weekday": 2, "hour": 8, "minute": 30}}})
            self.assertIn("# keep me", result["yaml"])
            self.assertIn("custom: yes", result["yaml"])
            self.assertEqual(result["data"]["schedule"]["hour"], 8)
            self.assertTrue(result["backups"])
            restored = restore_backup(path, result["backups"][0]["name"])
            self.assertNotIn("schedule:", restored["yaml"])


if __name__ == "__main__":
    unittest.main()
