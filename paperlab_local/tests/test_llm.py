import unittest
from unittest import mock
import json
import os
import tempfile
from pathlib import Path

from paperlab_local import relevance


class TestParseJson(unittest.TestCase):
    def test_plain(self):
        from paperlab_local.llm import _parse_json
        self.assertEqual(_parse_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        from paperlab_local.llm import _parse_json
        self.assertEqual(_parse_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_embedded(self):
        from paperlab_local.llm import _parse_json
        self.assertEqual(_parse_json('here you go: {"a": 1} thanks'),
                         {"a": 1})


class TestProviderConfig(unittest.TestCase):
    def test_zhipu_defaults_and_secret_is_not_returned(self):
        from paperlab_local.llm import public_provider_config, save_provider_config
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "papers.db"
            initial = public_provider_config(db)
            self.assertEqual(initial["active_provider"], "zhipu")
            self.assertEqual(initial["providers"]["zhipu"]["model"], "glm-5.2")
            result = save_provider_config(db, {
                "active_provider": "zhipu",
                "providers": {"zhipu": {
                    "model": "glm-test", "base_url": "https://example.test/v1",
                    "api_key": "secret-value",
                }},
            })
            self.assertTrue(result["providers"]["zhipu"]["has_api_key"])
            self.assertNotIn("api_key", result["providers"]["zhipu"])
            stored = json.loads((Path(temp) / "llm_config.json").read_text())
            self.assertEqual(stored["providers"]["zhipu"]["api_key"], "secret-value")
            self.assertEqual(os.stat(Path(temp) / "llm_config.json").st_mode & 0o777, 0o600)

    def test_legacy_config_migrates_to_zhipu_and_cc_switch(self):
        from paperlab_local.llm import public_provider_config
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "papers.db"
            (Path(temp) / "llm_config.json").write_text(json.dumps({
                "active_provider": "codex",
                "providers": {"gemini": {"api_key": "old-google-key"},
                              "codex": {"model": "proxy-model"}},
            }))
            result = public_provider_config(db)
            self.assertEqual(result["active_provider"], "zhipu")
            self.assertNotIn("gemini", result["providers"])
            self.assertEqual(result["providers"]["cc_switch"]["model"], "proxy-model")


class TestRelevance(unittest.TestCase):
    def test_threshold_filter(self):
        rows = [
            {"id": "thash:aaa", "title": "LLM Inference Engine", "abstract": "speeding up LLM serving"},
            {"id": "thash:bbb", "title": "Totally Unrelated Botany Study", "abstract": "plants"},
        ]
        with mock.patch.object(relevance, "chat_json") as m:
            m.return_value = {"scores": [
                {"id": "thash:aaa", "score": 0.9, "reason": "core fit"},
                {"id": "thash:bbb", "score": 0.1, "reason": "off topic"},
            ]}
            out = relevance.score_batch(rows, "llm", ["LLM serving"], model="m")
        self.assertEqual(out[0][0], "thash:aaa")
        self.assertGreaterEqual(out[0][1], 0.9)
        self.assertLess(out[1][1], 0.5)


if __name__ == "__main__":
    unittest.main()
