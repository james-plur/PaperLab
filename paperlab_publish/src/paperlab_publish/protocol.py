"""Publish implementation metadata for the wire protocol."""

PROTOCOL_VERSION = "1.1"
APP_VERSION = "0.4.0"
SCHEMA_VERSION = 11
CAPABILITIES = ("field_merge", "object_chunks", "markdown_history", "paper_summary", "note_sync")


def handshake() -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "app_version": APP_VERSION,
        "schema_version": SCHEMA_VERSION,
        "capabilities": list(CAPABILITIES),
    }
