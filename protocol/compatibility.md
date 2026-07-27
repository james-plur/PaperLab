# Compatibility policy

- The protocol major version changes when a field is removed, renamed, or its
  meaning changes.
- A minor version may only add optional fields or capabilities.
- Publish supports the current and previous major protocol versions.
- Local sends only capabilities advertised by Publish.
- Database schema versions are private to each application and are never used
  as cross-application compatibility checks.
- Entity IDs and change IDs are stable UUIDs. Server sequences are monotonic.
- Unknown entity types or operations are rejected without advancing the cursor.
- Repeated change IDs are idempotent.
- File objects are addressed by SHA-256 and verified before becoming visible.
