"""Publish implementation metadata for the wire protocol."""

PROTOCOL_VERSION = "1.0"
APP_VERSION = "0.3.0"
SCHEMA_VERSION = 9
CAPABILITIES = ("field_merge", "object_chunks", "markdown_history")


def handshake() -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "app_version": APP_VERSION,
        "schema_version": SCHEMA_VERSION,
        "capabilities": list(CAPABILITIES),
    }
