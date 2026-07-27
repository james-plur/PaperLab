import unittest

from paperlab_publish.protocol import handshake


class ProtocolHandshakeTests(unittest.TestCase):
    def test_handshake_has_independent_version_metadata(self):
        value = handshake()
        self.assertRegex(value["protocol_version"], r"^1\.\d+$")
        self.assertGreaterEqual(value["schema_version"], 1)
        self.assertIn("field_merge", value["capabilities"])


if __name__ == "__main__":
    unittest.main()
