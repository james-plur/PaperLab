# PaperLab synchronization protocol

This directory is a language-neutral contract, not a runtime package. Neither
application imports files from here. Each application owns its serializers,
database schema, migrations, and release cycle.

Current protocol: `1.0`. Implementations must ignore unknown optional fields,
negotiate capabilities, and reject unsupported major versions before applying
changes. A client may keep downloading compatible changes when upload is
disabled, but it must never discard its pending queue after an incompatibility.
