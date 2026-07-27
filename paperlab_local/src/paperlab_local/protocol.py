"""Local implementation metadata for the wire protocol."""

PROTOCOL_VERSION = "1.0"
APP_VERSION = "0.3.0"
SCHEMA_VERSION = 9
CAPABILITIES = frozenset({"field_merge", "object_chunks", "markdown_history"})


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
    return CAPABILITIES & frozenset(handshake.get("capabilities") or ())
