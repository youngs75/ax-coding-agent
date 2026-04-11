"""MemoryStore — SQLite + FTS5 backed persistent memory store."""

from __future__ import annotations

import sqlite3
from pathlib import Path
import structlog

from coding_agent.memory.schema import MemoryRecord

log = structlog.get_logger(__name__)

# ── DDL ──────────────────────────────────────────────────────────────────────

_CREATE_MEMORIES = """\
CREATE TABLE IF NOT EXISTS memories (
    id         TEXT PRIMARY KEY,
    layer      TEXT NOT NULL,
    category   TEXT NOT NULL,
    key        TEXT NOT NULL,
    content    TEXT NOT NULL,
    source     TEXT DEFAULT '',
    project_id TEXT,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(layer, project_id, key)
);
"""

_CREATE_FTS = """\
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
USING fts5(content, category, key, content=memories, content_rowid=rowid);
"""

# Triggers keep the FTS index in sync with the main table automatically.
_TRIGGERS = [
    """\
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, category, key)
    VALUES (new.rowid, new.content, new.category, new.key);
END;
""",
    """\
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category, key)
    VALUES ('delete', old.rowid, old.content, old.category, old.key);
END;
""",
    """\
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category, key)
    VALUES ('delete', old.rowid, old.content, old.category, old.key);
    INSERT INTO memories_fts(rowid, content, category, key)
    VALUES (new.rowid, new.content, new.category, new.key);
END;
""",
]


# ── Helpers ──────────────────────────────────────────────────────────────────


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    """Convert a sqlite3.Row into a MemoryRecord."""
    return MemoryRecord(
        id=row["id"],
        layer=row["layer"],
        category=row["category"],
        key=row["key"],
        content=row["content"],
        source=row["source"] or "",
        project_id=row["project_id"],
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
    )


class MemoryStore:
    """Persistent memory store backed by SQLite with FTS5 full-text search.

    Thread-safety: each public method acquires its own connection-level
    transaction so concurrent calls from different threads are safe with
    ``check_same_thread=False``.
    """

    def __init__(self, db_path: str) -> None:
        # Ensure the parent directory exists.
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()
        log.info("memory_store.initialized", db_path=db_path)

    # ── Schema bootstrap ─────────────────────────────────────────────────

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(_CREATE_MEMORIES)
        cur.execute(_CREATE_FTS)
        for trigger_sql in _TRIGGERS:
            cur.execute(trigger_sql)
        self._conn.commit()

    # ── Public API ───────────────────────────────────────────────────────

    def upsert(self, record: MemoryRecord) -> None:
        """Insert or replace a memory record, keeping FTS in sync via triggers."""
        record.touch()
        try:
            self._conn.execute(
                """\
                INSERT INTO memories (id, layer, category, key, content, source,
                                      project_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(layer, project_id, key) DO UPDATE SET
                    content    = excluded.content,
                    category   = excluded.category,
                    source     = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    record.id,
                    record.layer,
                    record.category,
                    record.key,
                    record.content,
                    record.source,
                    record.project_id or "",
                    record.created_at,
                    record.updated_at,
                ),
            )
            self._conn.commit()
            log.debug("memory_store.upserted", key=record.key, layer=record.layer)
        except sqlite3.Error:
            self._conn.rollback()
            log.exception("memory_store.upsert_failed", key=record.key)
            raise

    def search(
        self,
        query: str,
        layer: str | None = None,
        limit: int = 10,
    ) -> list[MemoryRecord]:
        """Full-text search across memories via FTS5.

        If *layer* is provided the results are further filtered to that layer.
        """
        if not query or not query.strip():
            return []

        try:
            # FTS5 match query — escape double-quotes in user input.
            safe_query = query.replace('"', '""')
            sql = """\
                SELECT m.*
                FROM memories m
                JOIN memories_fts f ON m.rowid = f.rowid
                WHERE memories_fts MATCH ?
            """
            params: list[object] = [f'"{safe_query}"']

            if layer:
                sql += " AND m.layer = ?"
                params.append(layer)

            sql += " ORDER BY rank LIMIT ?"
            params.append(limit)

            rows = self._conn.execute(sql, params).fetchall()
            return [_row_to_record(r) for r in rows]
        except sqlite3.Error:
            log.exception("memory_store.search_failed", query=query)
            return []

    def get_by_layer(
        self,
        layer: str,
        project_id: str | None = None,
    ) -> list[MemoryRecord]:
        """Return all records for a given layer, optionally filtered by project."""
        try:
            if project_id is not None:
                rows = self._conn.execute(
                    "SELECT * FROM memories WHERE layer = ? AND project_id = ? "
                    "ORDER BY updated_at DESC",
                    (layer, project_id),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM memories WHERE layer = ? ORDER BY updated_at DESC",
                    (layer,),
                ).fetchall()
            return [_row_to_record(r) for r in rows]
        except sqlite3.Error:
            log.exception("memory_store.get_by_layer_failed", layer=layer)
            return []

    def delete(self, record_id: str) -> bool:
        """Delete a memory by its id. Returns True if a row was deleted."""
        try:
            cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (record_id,))
            self._conn.commit()
            deleted = cur.rowcount > 0
            if deleted:
                log.debug("memory_store.deleted", record_id=record_id)
            return deleted
        except sqlite3.Error:
            self._conn.rollback()
            log.exception("memory_store.delete_failed", record_id=record_id)
            return False

    def list_all(self) -> list[MemoryRecord]:
        """Return every record in the store."""
        try:
            rows = self._conn.execute(
                "SELECT * FROM memories ORDER BY layer, updated_at DESC"
            ).fetchall()
            return [_row_to_record(r) for r in rows]
        except sqlite3.Error:
            log.exception("memory_store.list_all_failed")
            return []

    def get_existing_keys(self) -> set[str]:
        """Return the set of all existing keys (used to prevent duplicates)."""
        try:
            rows = self._conn.execute("SELECT key FROM memories").fetchall()
            return {r["key"] for r in rows}
        except sqlite3.Error:
            log.exception("memory_store.get_existing_keys_failed")
            return set()

    def rebuild_fts(self) -> None:
        """Manually rebuild the FTS index from the main table."""
        try:
            self._conn.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
            self._conn.commit()
            log.info("memory_store.fts_rebuilt")
        except sqlite3.Error:
            self._conn.rollback()
            log.exception("memory_store.fts_rebuild_failed")

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()
        log.info("memory_store.closed")
