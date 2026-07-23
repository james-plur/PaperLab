"""SQLite source of truth, schema migration, deduplication and stage state."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 6


def normalize_title(title: str) -> str:
    value = (title or "").lower()
    value = re.sub(r"[^\w\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def title_hash(title: str) -> str:
    return hashlib.sha1(normalize_title(title).encode("utf-8")).hexdigest()[:12]


def _clean_arxiv(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(\d{4}\.\d{4,5})(?:v\d+)?", value)
    return match.group(1) if match else value.strip() or None


def compute_id(*, arxiv_id: str | None = None, doi: str | None = None,
               title: str | None = None) -> str:
    if arxiv_id:
        return "arxiv:" + str(_clean_arxiv(arxiv_id))
    if doi:
        return "doi:" + doi.strip().lower()
    if title:
        return "thash:" + title_hash(title)
    raise ValueError("cannot compute id: need arxiv_id, doi, or title")


SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
  id TEXT PRIMARY KEY,
  arxiv_id TEXT,
  doi TEXT,
  norm_title_hash TEXT,
  title TEXT NOT NULL,
  authors TEXT,
  organizations TEXT,
  awards TEXT,
  venue TEXT,
  year INTEGER,
  track TEXT,
  topics TEXT,
  abstract TEXT,
  citation_count INTEGER,
  relevance_score REAL,
  relevance_reason TEXT,
  pdf_path TEXT,
  pdf_status TEXT,
  pdf_url TEXT,
  github_url TEXT,
  target_problem TEXT,
  core_idea TEXT,
  figure_kind TEXT,
  figure_path TEXT,
  figure_mermaid TEXT,
  discovered_at REAL,
  last_updated REAL,
  extraction_version INTEGER,
  published_date TEXT,
  source_url TEXT,
  metadata_status TEXT DEFAULT 'pending',
  authors_json TEXT,
  organizations_json TEXT,
  obsidian_path TEXT,
  obsidian_hash TEXT
);
CREATE TABLE IF NOT EXISTS paper_sources (
  paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  source_id TEXT NOT NULL DEFAULT '',
  landing_url TEXT,
  pdf_url TEXT,
  raw_json TEXT,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  PRIMARY KEY (paper_id, source, source_id)
);
CREATE TABLE IF NOT EXISTS paper_topics (
  paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  topic_id TEXT NOT NULL,
  score REAL NOT NULL,
  relevant INTEGER NOT NULL,
  reason TEXT,
  model TEXT,
  definition_hash TEXT NOT NULL,
  prompt_version INTEGER NOT NULL,
  scored_at REAL NOT NULL,
  PRIMARY KEY (paper_id, topic_id)
);
CREATE TABLE IF NOT EXISTS paper_venues (
  paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  venue TEXT NOT NULL,
  year INTEGER NOT NULL,
  track TEXT,
  source TEXT,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  PRIMARY KEY (paper_id, venue, year)
);
CREATE TABLE IF NOT EXISTS venue_sync (
  venue TEXT NOT NULL,
  year INTEGER NOT NULL,
  status TEXT NOT NULL,
  paper_count INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  checksum TEXT,
  last_attempt REAL NOT NULL,
  last_success REAL,
  PRIMARY KEY (venue, year)
);
CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  started_at REAL NOT NULL,
  finished_at REAL,
  status TEXT NOT NULL,
  details TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(pdf_status);
CREATE INDEX IF NOT EXISTS idx_papers_venue ON papers(venue, year);
CREATE INDEX IF NOT EXISTS idx_papers_arxiv ON papers(arxiv_id);
CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_thash ON papers(norm_title_hash);
CREATE INDEX IF NOT EXISTS idx_topic_relevant ON paper_topics(topic_id, relevant, score);
CREATE INDEX IF NOT EXISTS idx_source_lookup ON paper_sources(source, source_id);
CREATE INDEX IF NOT EXISTS idx_paper_venue_membership ON paper_venues(venue, year, paper_id);
CREATE TABLE IF NOT EXISTS paper_reports (
  paper_id TEXT PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
  report_path TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ok',
  generated_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
"""

_V2_COLUMNS = {
    "published_date": "TEXT",
    "source_url": "TEXT",
    "metadata_status": "TEXT DEFAULT 'pending'",
    "authors_json": "TEXT",
    "organizations_json": "TEXT",
    "obsidian_path": "TEXT",
    "obsidian_hash": "TEXT",
    "awards": "TEXT",
    "keywords": "TEXT",
}


class Store:
    def __init__(self, db_path: str | Path, *, backup_on_migrate: bool = True):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if backup_on_migrate and self._needs_migration():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(self.db_path, self.db_path.with_suffix(f".db.bak-{stamp}"))
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._ensure_columns()
        self._migrate_legacy_rows()
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()

    def _needs_migration(self) -> bool:
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return False
        conn = sqlite3.connect(self.db_path)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return "papers" in tables and version < SCHEMA_VERSION
        finally:
            conn.close()

    def _ensure_columns(self) -> None:
        existing = {r[1] for r in self.conn.execute("PRAGMA table_info(papers)")}
        for name, decl in _V2_COLUMNS.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE papers ADD COLUMN {name} {decl}")

    def _migrate_legacy_rows(self) -> None:
        """Move the v1 import bucket out of the relevance-topic field."""
        now = time.time()
        self.conn.execute(
            """INSERT OR IGNORE INTO paper_sources
               (paper_id,source,source_id,landing_url,pdf_url,raw_json,first_seen,last_seen)
               SELECT id,'legacy-conf-import',id,source_url,pdf_url,NULL,
                      COALESCE(discovered_at,?),COALESCE(last_updated,?)
               FROM papers WHERE ','||topics||',' LIKE '%,conf2026,%'""",
            (now, now),
        )
        self.conn.execute(
            """UPDATE papers SET topics=trim(replace(','||topics||',', ',conf2026,', ','), ',')
               WHERE ','||topics||',' LIKE '%,conf2026,%'"""
        )
        self.conn.execute(
            """INSERT OR IGNORE INTO paper_venues
               (paper_id,venue,year,track,source,first_seen,last_seen)
               SELECT id,venue,year,track,'papers',COALESCE(discovered_at,?),COALESCE(last_updated,?)
               FROM papers WHERE venue IS NOT NULL AND venue!='' AND year IS NOT NULL""",
            (now, now),
        )
        self.conn.execute(
            """INSERT OR IGNORE INTO paper_venues
               (paper_id,venue,year,track,source,first_seen,last_seen)
               SELECT paper_id,upper(json_extract(raw_json,'$.venue')),
                      cast(json_extract(raw_json,'$.year') AS INTEGER),
                      json_extract(raw_json,'$.track'),source,first_seen,last_seen
               FROM paper_sources
               WHERE json_valid(raw_json)
                 AND json_extract(raw_json,'$.venue') IS NOT NULL
                 AND json_extract(raw_json,'$.year') IS NOT NULL"""
        )


    def get_keywords(self, paper_id: str) -> list[str]:
        row = self.conn.execute(
            "SELECT keywords FROM papers WHERE id=?", (paper_id,)).fetchone()
        if not row or not row["keywords"]:
            return []
        return [k.strip() for k in row["keywords"].split(",") if k.strip()]

    def set_keywords(self, paper_id: str, keywords: list[str]) -> None:
        kw = ",".join(sorted(set(k.strip() for k in keywords if k.strip())))
        self.conn.execute(
            "UPDATE papers SET keywords=?, last_updated=? WHERE id=?",
            (kw, time.time(), paper_id))
        self.conn.commit()

    def add_keywords(self, paper_id: str, keywords: list[str]) -> None:
        existing = set(self.get_keywords(paper_id))
        existing.update(k.strip() for k in keywords if k.strip())
        self.set_keywords(paper_id, list(existing))

    def papers_without_keywords(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT id, title, abstract FROM papers "
               "WHERE (keywords IS NULL OR keywords='') AND abstract IS NOT NULL AND trim(abstract)!='' "
               "ORDER BY year DESC, discovered_at DESC")
        if limit:
            sql += " LIMIT ?"
            return self.conn.execute(sql, (limit,)).fetchall()
        return self.conn.execute(sql).fetchall()

    def search_by_keywords(self, query: str, limit: int = 50) -> list[sqlite3.Row]:
        """Full-text search on title, abstract, and keywords."""
        token = f"%{query.lower()}%"
        return self.conn.execute(
            """SELECT * FROM papers
               WHERE lower(title) LIKE ? OR lower(abstract) LIKE ? OR lower(keywords) LIKE ?
               ORDER BY year DESC, title LIMIT ?""",
            (token, token, token, limit)).fetchall()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @staticmethod
    def _add_topic(existing: str | None, topic: str) -> str:
        values = {x for x in (existing or "").split(",") if x}
        values.add(topic)
        return ",".join(sorted(values))

    def _find_existing(self, arxiv_id: str | None, doi: str | None,
                       normalized_hash: str) -> sqlite3.Row | None:
        if arxiv_id:
            row = self.conn.execute("SELECT * FROM papers WHERE arxiv_id=?", (arxiv_id,)).fetchone()
            if row:
                return row
        if doi:
            # DOI values are normalized to lowercase before insertion.  An
            # exact comparison lets SQLite use idx_papers_doi; wrapping the
            # column in lower() forced a full 90k-row scan for every paper.
            row = self.conn.execute("SELECT * FROM papers WHERE doi=?", (doi.lower(),)).fetchone()
            if row:
                return row
        return self.conn.execute(
            "SELECT * FROM papers WHERE norm_title_hash=?", (normalized_hash,)
        ).fetchone()

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not value:
            return []
        if isinstance(value, list):
            return [str(x).strip() for x in value if str(x).strip()]
        return [x.strip() for x in re.split(r"\s*;\s*|\s*,\s*", str(value)) if x.strip()]

    def upsert_candidate(self, candidate: dict, topic_name: str | None = None,
                         *, commit: bool = True) -> str:
        title = str(candidate.get("title") or "").strip()
        if not title:
            raise ValueError("candidate missing title")
        arxiv_id = _clean_arxiv(candidate.get("arxiv_id"))
        doi = str(candidate.get("doi") or "").strip().lower() or None
        normalized_hash = title_hash(title)
        now = time.time()
        row = self._find_existing(arxiv_id, doi, normalized_hash)
        if row is None:
            paper_id = compute_id(arxiv_id=arxiv_id, doi=doi, title=title)
            authors = candidate.get("authors")
            organizations = candidate.get("organizations")
            self.conn.execute(
                """INSERT INTO papers
                (id, arxiv_id, doi, norm_title_hash, title, authors, organizations, awards,
                 venue, year, track, topics, abstract, citation_count, pdf_status,
                 pdf_url, discovered_at, last_updated, published_date, source_url,
                 metadata_status, authors_json, organizations_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (paper_id, arxiv_id, doi, normalized_hash, title, authors, organizations,
                 candidate.get("awards"), candidate.get("venue"), candidate.get("year"), candidate.get("track"),
                 topic_name or "", candidate.get("abstract"), candidate.get("citation_count"),
                 None, candidate.get("pdf_url"), now, now, candidate.get("published_date"),
                 candidate.get("source_url") or candidate.get("url"),
                 "complete" if candidate.get("abstract") else "pending",
                 json.dumps(self._string_list(authors), ensure_ascii=False),
                 json.dumps(self._string_list(organizations), ensure_ascii=False)),
            )
        else:
            paper_id = row["id"]
            self._merge_into(paper_id, row, candidate, topic_name, arxiv_id, doi, now)
        source = str(candidate.get("source") or candidate.get("venue") or "catalog").lower()
        source_id = str(candidate.get("source_id") or candidate.get("paper_id") or "")
        self.upsert_source(
            paper_id, source=source, source_id=source_id,
            landing_url=candidate.get("source_url") or candidate.get("url"),
            pdf_url=candidate.get("pdf_url"), raw=candidate,
        )
        if candidate.get("venue") and candidate.get("year"):
            self.upsert_venue(
                paper_id, venue=str(candidate["venue"]).upper(), year=int(candidate["year"]),
                track=candidate.get("track"), source=source,
            )
        if commit:
            self.conn.commit()
        return paper_id

    def upsert_venue(self, paper_id: str, *, venue: str, year: int,
                     track: str | None = None, source: str = "catalog") -> None:
        now = time.time()
        self.conn.execute(
            """INSERT INTO paper_venues
               (paper_id,venue,year,track,source,first_seen,last_seen)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(paper_id,venue,year) DO UPDATE SET
                 track=COALESCE(excluded.track,paper_venues.track),
                 source=excluded.source,last_seen=excluded.last_seen""",
            (paper_id, venue.upper(), int(year), track, source, now, now),
        )

    def _merge_into(self, paper_id: str, row: sqlite3.Row, candidate: dict,
                    topic_name: str | None, arxiv_id: str | None, doi: str | None,
                    now: float) -> None:
        updates: dict[str, Any] = {"last_updated": now}
        if topic_name:
            updates["topics"] = self._add_topic(row["topics"], topic_name)
        if arxiv_id and not row["arxiv_id"]:
            updates["arxiv_id"] = arxiv_id
        if doi and not row["doi"]:
            updates["doi"] = doi
        venue = candidate.get("venue")
        candidate_source = str(candidate.get("source") or "").lower()
        # The legacy conf2026 import assigned year=2026 to every row.  The
        # first later catalogue record is authoritative for venue/year and
        # must be allowed to correct that provisional label.  Once any
        # non-legacy catalogue source exists, do not keep flipping identity.
        legacy_only = self.conn.execute(
            """SELECT EXISTS(SELECT 1 FROM paper_sources WHERE paper_id=? AND source='legacy-conf-import')
                      AND NOT EXISTS(SELECT 1 FROM paper_sources WHERE paper_id=?
                                     AND source!='legacy-conf-import'
                                     AND json_valid(raw_json)
                                     AND json_extract(raw_json,'$.venue') IS NOT NULL
                                     AND json_extract(raw_json,'$.year') IS NOT NULL)""",
            (paper_id, paper_id),
        ).fetchone()[0]
        if (legacy_only and candidate_source != "legacy-conf-import"):
            if venue:
                updates["venue"] = venue
            if candidate.get("year"):
                updates["year"] = candidate.get("year")
        if venue and (not row["venue"] or (row["venue"] == "arXiv" and venue != "arXiv")):
            updates["venue"] = venue
        for key in ("authors", "organizations", "awards", "year", "track", "abstract",
                    "citation_count", "pdf_url", "published_date", "source_url"):
            value = candidate.get(key)
            if key == "source_url" and not value:
                value = candidate.get("url")
            if value not in (None, "") and row[key] in (None, ""):
                updates[key] = value
        if updates.get("authors"):
            updates["authors_json"] = json.dumps(self._string_list(updates["authors"]), ensure_ascii=False)
        if updates.get("organizations"):
            updates["organizations_json"] = json.dumps(self._string_list(updates["organizations"]), ensure_ascii=False)
        if updates.get("abstract") or row["abstract"]:
            updates["metadata_status"] = "complete"
        self.conn.execute(
            "UPDATE papers SET " + ", ".join(f"{key}=?" for key in updates) + " WHERE id=?",
            (*updates.values(), paper_id),
        )

    def upsert_source(self, paper_id: str, *, source: str, source_id: str = "",
                      landing_url: str | None = None, pdf_url: str | None = None,
                      raw: dict | None = None) -> None:
        now = time.time()
        self.conn.execute(
            """INSERT INTO paper_sources
               (paper_id,source,source_id,landing_url,pdf_url,raw_json,first_seen,last_seen)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(paper_id,source,source_id) DO UPDATE SET
                 landing_url=COALESCE(excluded.landing_url,paper_sources.landing_url),
                 pdf_url=COALESCE(excluded.pdf_url,paper_sources.pdf_url),
                 raw_json=COALESCE(excluded.raw_json,paper_sources.raw_json),
                 last_seen=excluded.last_seen""",
            (paper_id, source, source_id, landing_url, pdf_url,
             json.dumps(raw, ensure_ascii=False, default=str) if raw else None, now, now),
        )

    def set_enrichment(self, paper_id: str, fields: dict, *, commit: bool = True) -> None:
        row = self.get(paper_id)
        if row is None:
            return
        allowed = ("arxiv_id", "doi", "abstract", "citation_count", "github_url",
                   "pdf_url", "organizations", "authors", "venue", "year",
                   "published_date", "source_url", "metadata_status", "awards")
        updates: dict[str, Any] = {}
        for key in allowed:
            if fields.get(key) not in (None, ""):
                value = _clean_arxiv(fields[key]) if key == "arxiv_id" else fields[key]
                if row[key] in (None, "") or key in ("citation_count", "metadata_status"):
                    updates[key] = value
        if updates.get("authors"):
            updates["authors_json"] = json.dumps(self._string_list(updates["authors"]), ensure_ascii=False)
        if updates.get("organizations"):
            updates["organizations_json"] = json.dumps(self._string_list(updates["organizations"]), ensure_ascii=False)
        if fields.get("topic"):
            updates["topics"] = self._add_topic(row["topics"], fields["topic"])
        if not updates:
            return
        updates["last_updated"] = time.time()
        self.conn.execute(
            "UPDATE papers SET " + ", ".join(f"{key}=?" for key in updates) + " WHERE id=?",
            (*updates.values(), paper_id),
        )
        if commit:
            self.conn.commit()

    def pending_metadata(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = """SELECT * FROM papers
                 WHERE (metadata_status!='complete'
                    OR abstract IS NULL OR trim(abstract)=''
                    OR organizations IS NULL OR trim(organizations)='')
                 AND NOT EXISTS (
                   SELECT 1 FROM paper_sources ps
                   WHERE ps.paper_id=papers.id AND ps.source='paper-search-mcp-attempt')
                 ORDER BY year DESC, discovered_at"""
        if limit is not None:
            sql += " LIMIT ?"
            return self.conn.execute(sql, (limit,)).fetchall()
        return self.conn.execute(sql).fetchall()

    def set_topic_score(self, paper_id: str, topic_id: str, score: float, reason: str,
                        *, threshold: float, model: str, definition_hash: str,
                        prompt_version: int) -> None:
        score = max(0.0, min(1.0, float(score)))
        self.conn.execute(
            """INSERT INTO paper_topics
               (paper_id,topic_id,score,relevant,reason,model,definition_hash,prompt_version,scored_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(paper_id,topic_id) DO UPDATE SET
                 score=excluded.score,relevant=excluded.relevant,reason=excluded.reason,
                 model=excluded.model,definition_hash=excluded.definition_hash,
                 prompt_version=excluded.prompt_version,scored_at=excluded.scored_at""",
            (paper_id, topic_id, score, int(score >= threshold), reason[:500], model,
             definition_hash, prompt_version, time.time()),
        )
        # Maintain legacy columns for old index/extract code when there is only
        # one visible score, but v2 logic never reads these for topic decisions.
        self.conn.execute(
            "UPDATE papers SET relevance_score=?, relevance_reason=?, last_updated=? WHERE id=?",
            (score, reason[:500], time.time(), paper_id),
        )
        self.conn.commit()

    def set_relevance(self, paper_id: str, score: float, reason: str = "") -> None:
        """Backward-compatible v1 relevance setter."""
        self.conn.execute(
            "UPDATE papers SET relevance_score=?, relevance_reason=?, last_updated=? WHERE id=?",
            (score, reason, time.time(), paper_id),
        )
        self.conn.commit()

    def pending_topic(self, topic_id: str, definition_hash: str, prompt_version: int,
                      *, force: bool = False) -> list[sqlite3.Row]:
        if force:
            return self.all()
        return self.conn.execute(
            """SELECT p.* FROM papers p LEFT JOIN paper_topics pt
               ON pt.paper_id=p.id AND pt.topic_id=?
               WHERE pt.paper_id IS NULL OR pt.definition_hash!=? OR pt.prompt_version!=?
               ORDER BY p.year DESC,p.title""",
            (topic_id, definition_hash, prompt_version),
        ).fetchall()

    def topic_scores(self, paper_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM paper_topics WHERE paper_id=? ORDER BY topic_id", (paper_id,)
        ).fetchall()

    def topic_papers_v2(self, topic_id: str, *, min_score: float | None = None,
                        relevant_only: bool = True) -> list[sqlite3.Row]:
        clauses = ["pt.topic_id=?"]
        params: list[Any] = [topic_id]
        if relevant_only:
            clauses.append("pt.relevant=1")
        if min_score is not None:
            clauses.append("pt.score>=?")
            params.append(min_score)
        return self.conn.execute(
            """SELECT p.*,pt.score AS topic_score,pt.relevant AS topic_relevant,
                      pt.reason AS topic_reason
               FROM papers p JOIN paper_topics pt ON pt.paper_id=p.id
               WHERE """ + " AND ".join(clauses) +
            " ORDER BY pt.score DESC,p.year DESC,p.title",
            params,
        ).fetchall()

    def has_topic_scores(self, topic_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM paper_topics WHERE topic_id=? LIMIT 1", (topic_id,)
        ).fetchone() is not None

    def set_pdf(self, paper_id: str, *, path: str | None, status: str,
                pdf_url: str | None = None) -> None:
        self.conn.execute(
            """UPDATE papers SET pdf_path=?,pdf_status=?,pdf_url=COALESCE(?,pdf_url),
               last_updated=? WHERE id=?""",
            (path, status, pdf_url, time.time(), paper_id),
        )
        self.conn.commit()

    def set_extraction(self, paper_id: str, *, target_problem: str | None,
                       core_idea: str | None, organizations: str | None,
                       github_url: str | None, figure_kind: str | None,
                       figure_path: str | None = None,
                       figure_mermaid: str | None = None, version: int = 1) -> None:
        self.conn.execute(
            """UPDATE papers SET target_problem=?,core_idea=?,organizations=?,github_url=?,
               figure_kind=?,figure_path=?,figure_mermaid=?,extraction_version=?,last_updated=?
               WHERE id=?""",
            (target_problem, core_idea, organizations, github_url, figure_kind,
             figure_path, figure_mermaid, version, time.time(), paper_id),
        )
        self.conn.commit()

    def set_obsidian(self, paper_id: str, path: str, content_hash: str,
                     *, commit: bool = True) -> None:
        self.conn.execute(
            "UPDATE papers SET obsidian_path=?,obsidian_hash=? WHERE id=?",
            (path, content_hash, paper_id),
        )
        if commit:
            self.conn.commit()

    def get(self, paper_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()

    def all(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM papers ORDER BY year DESC,relevance_score DESC,title"
        ).fetchall()

    def topic_papers(self, topic: str) -> list[sqlite3.Row]:
        # Prefer v2 scores; fall back to legacy comma-separated collection topics.
        if self.has_topic_scores(topic):
            return self.topic_papers_v2(topic, relevant_only=False)
        return self.conn.execute(
            "SELECT * FROM papers WHERE ','||topics||',' LIKE ? ORDER BY year DESC,title",
            (f"%,{topic},%",),
        ).fetchall()

    def pending(self, topic: str, stage: str) -> list[sqlite3.Row]:
        rows = self.topic_papers(topic)
        output = []
        for row in rows:
            if stage == "relevance" and row["relevance_score"] is None:
                output.append(row)
            elif stage == "download" and row["relevance_score"] is not None and row["pdf_status"] is None:
                output.append(row)
            elif stage == "extract" and row["pdf_status"] == "ok" and row["extraction_version"] is None:
                output.append(row)
        return output

    def record_venue_sync(self, venue: str, year: int, *, status: str,
                          count: int = 0, error: str = "", checksum: str = "") -> None:
        now = time.time()
        self.conn.execute(
            """INSERT INTO venue_sync
               (venue,year,status,paper_count,error,checksum,last_attempt,last_success)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(venue,year) DO UPDATE SET status=excluded.status,
                 paper_count=excluded.paper_count,error=excluded.error,checksum=excluded.checksum,
                 last_attempt=excluded.last_attempt,
                 last_success=COALESCE(excluded.last_success,venue_sync.last_success)""",
            (venue, year, status, count, error[:1000], checksum, now,
             now if status == "ok" else None),
        )
        self.conn.commit()

    def stats(self, topic: str | None = None) -> dict:
        rows = self.topic_papers_v2(topic, relevant_only=False) if topic and self.has_topic_scores(topic) else (
            self.topic_papers(topic) if topic else self.all())
        return {
            "total": len(rows),
            "scored": sum(1 for row in rows if ("topic_score" in row.keys() or row["relevance_score"] is not None)),
            "relevant": (sum(1 for row in rows if row["topic_relevant"] == 1)
                         if rows and "topic_relevant" in rows[0].keys() else None),
            "downloaded": sum(1 for row in rows if row["pdf_status"] == "ok"),
            "extracted": sum(1 for row in rows if row["extraction_version"] is not None),
        }

    def get_report(self, paper_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM paper_reports WHERE paper_id=?", (paper_id,)).fetchone()

    def set_report(self, paper_id: str, *, report_path: str, status: str = "ok",
                   commit: bool = True) -> None:
        now = time.time()
        self.conn.execute(
            """INSERT INTO paper_reports (paper_id,report_path,status,generated_at,updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(paper_id) DO UPDATE SET
                 report_path=excluded.report_path,status=excluded.status,
                 updated_at=excluded.updated_at""",
            (paper_id, str(report_path), status, now, now),
        )
        if commit:
            self.conn.commit()
