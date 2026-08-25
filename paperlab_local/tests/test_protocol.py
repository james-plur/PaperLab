import unittest

from paperlab_local.protocol import handshake, require_compatible


class ProtocolCompatibilityTests(unittest.TestCase):
    def test_current_major_negotiates_shared_capabilities(self):
        remote = handshake() | {"capabilities": ["field_merge", "paper_summary", "note_sync", "future_feature"]}
        self.assertEqual(require_compatible(remote), {"field_merge", "paper_summary", "note_sync"})

    def test_missing_summary_and_note_capabilities_requires_upgrade(self):
        remote = handshake() | {"capabilities": ["field_merge"]}
        with self.assertRaisesRegex(RuntimeError, "missing capabilities"):
            require_compatible(remote)

    def test_unknown_major_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "incompatible sync protocol"):
            require_compatible({"protocol_version": "2.0", "capabilities": []})


if __name__ == "__main__":
    unittest.main()
