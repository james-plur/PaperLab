"""Add normalized_orgs column and populate it from existing organization data."""
from __future__ import annotations
import json
import time
from .org_normalizer import split_and_normalize

SCHEMA_VERSION_ADD = 7


def needs_org_migration(db_path) -> bool:
    """Check if the normalized_orgs column exists."""
    import sqlite3
    if not db_path.exists() or db_path.stat().st_size == 0:
        return False
    conn = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)")}
        return "normalized_orgs" not in cols
    finally:
        conn.close()


def migrate_add_normalized_orgs(conn):
    """Add normalized_orgs column to papers table."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(papers)")}
    if "normalized_orgs" not in existing:
        conn.execute("ALTER TABLE papers ADD COLUMN normalized_orgs TEXT")
        conn.commit()


def populate_normalized_orgs(store, *, limit=None, log=print):
    """Populate normalized_orgs from organizations + organizations_json fields.

    Also attempts to extract orgs from the authors field for papers that lack
    structured organization data.
    """
    from .org_normalizer import split_and_normalize, _clean_name, _SPLIT_RE

    sql = ("SELECT id, authors, organizations, organizations_json, normalized_orgs "
           "FROM papers WHERE (normalized_orgs IS NULL OR trim(normalized_orgs)=\'\')")
    if limit:
        sql += f" LIMIT {limit}"
    rows = store.conn.execute(sql).fetchall()
    total = len(rows)
    if not total:
        log("  [orgs] nothing to normalize")
        return 0
    log(f"  [orgs] normalizing {total} papers...")
    done = 0
    import re
    for i, row in enumerate(rows):
        orgs_list = []
        # Primary: use organizations_json
        if row["organizations_json"]:
            try:
                raw_orgs = json.loads(row["organizations_json"])
                if isinstance(raw_orgs, list):
                    for o in raw_orgs:
                        if o and str(o).strip():
                            orgs_list.append(str(o).strip())
            except (ValueError, TypeError):
                pass
        # Fallback: split organizations field
        if not orgs_list and row["organizations"]:
            orgs_list = _SPLIT_RE.split(row["organizations"])

        # Last resort: try extracting from authors field
        if not orgs_list and row["authors"]:
            # Patterns: "Name, University Name" or "Name (University Name)"
            authors = row["authors"]
            # Match "Name, Org" or "Name (Org)" patterns
            for m in re.finditer(r"[,;)]\s*([A-Z][^,;()]{5,80})$", authors, re.M):
                org_str = m.group(1).strip()
                if org_str and len(org_str) > 5:
                    orgs_list.append(org_str)
            # Also match "(Org Name, Country)" patterns
            for m in re.finditer(r"\(([A-Z][^)]{5,80})\)", authors):
                org_str = m.group(1).strip()
                if org_str and len(org_str) > 5:
                    # Strip trailing country
                    org_str = re.sub(r",\s*[A-Z][a-z]+\s*$", "", org_str)
                    orgs_list.append(org_str)

        # Normalize and deduplicate
        normalized = []
        seen = set()
        for o in orgs_list:
            for n in split_and_normalize(o):
                key = n.lower()
                if key not in seen:
                    seen.add(key)
                    normalized.append(n)

        if normalized:
            store.conn.execute(
                "UPDATE papers SET normalized_orgs=? WHERE id=?",
                (json.dumps(normalized, ensure_ascii=False), row["id"]),
            )
            done += 1

        if (i + 1) % 5000 == 0:
            store.conn.commit()
            log(f"  [orgs] {i+1}/{total} processed, {done} updated")

    store.conn.commit()
    log(f"  [orgs] done: {done}/{total} papers got normalized orgs")
    return done
