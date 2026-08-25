"""Local implementation metadata for the wire protocol."""

PROTOCOL_VERSION = "1.1"
APP_VERSION = "0.4.0"
SCHEMA_VERSION = 11
CAPABILITIES = frozenset({
    "field_merge", "object_chunks", "markdown_history", "paper_summary", "note_sync",
})
REQUIRED_CAPABILITIES = frozenset({"paper_summary", "note_sync"})


def handshake() -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "app_version": APP_VERSION,
        "schema_version": SCHEMA_VERSION,
        "capabilities": sorted(CAPABILITIES),
    }


def require_compatible(handshake: dict) -> frozenset[str]:
    remote = str(handshake.get("protocol_version") or "")
    if remote.split(".", 1)[0] != PROTOCOL_VERSION.split(".", 1)[0]:
        raise RuntimeError(
            f"incompatible sync protocol: local {PROTOCOL_VERSION}, remote {remote or 'missing'}"
        )
    available = frozenset(handshake.get("capabilities") or ())
    missing = REQUIRED_CAPABILITIES - available
    if missing:
        raise RuntimeError("sync server must be upgraded; missing capabilities: " + ", ".join(sorted(missing)))
    return CAPABILITIES & available
