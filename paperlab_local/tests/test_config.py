import tempfile
import unittest
from pathlib import Path

from paperlab_local.config import load_config


class ConfigPortabilityTests(unittest.TestCase):
    def test_relative_runtime_paths_resolve_from_config_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "topics.yaml"
            config.write_text("pdf_root: runtime\ndb_path: state/papers.db\n", encoding="utf-8")
            loaded = load_config(config)
            self.assertEqual(loaded.pdf_root, (root / "runtime").resolve())
            self.assertEqual(loaded.db_path, (root / "state/papers.db").resolve())


if __name__ == "__main__":
    unittest.main()
