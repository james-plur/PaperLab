import unittest
from unittest import mock

from paperlab import relevance


class TestParseJson(unittest.TestCase):
    def test_plain(self):
        from paperlab.llm import _parse_json
        self.assertEqual(_parse_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        from paperlab.llm import _parse_json
        self.assertEqual(_parse_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_embedded(self):
        from paperlab.llm import _parse_json
        self.assertEqual(_parse_json('here you go: {"a": 1} thanks'),
                         {"a": 1})


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
