"""MemoryMiddleware — LangGraph node functions for memory injection and extraction.

Backed by ``minyoung_mah.SqliteMemoryStore`` (async). Keeps the
3-layer ``user`` / ``project`` / ``domain`` semantics on top of the library's
``tier``/``scope`` schema: ``layer→tier``, ``project_id→scope``.
"""

from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
from typing import Any

import structlog

from coding_agent.core.state import AgentState
from coding_agent.memory.extractor import MemoryExtractor
from coding_agent.memory.schema import MemoryRecord, entry_to_record, record_to_entry
from minyoung_mah import MemoryStore

log = structlog.get_logger(__name__)

_TOPIC_SIMILARITY_THRESHOLD = 0.6  # below this → topic changed, re-search domain


def _format_records(records: list[MemoryRecord]) -> str:
    if not records:
        return "(none)"
    lines: list[str] = []
    for rec in records:
        lines.append(f"- [{rec.category}] {rec.key}: {rec.content}")
    return "\n".join(lines)


class MemoryMiddleware:
    """Async LangGraph node functions for injecting and extracting memories."""

    def __init__(self, store: MemoryStore, extractor: MemoryExtractor) -> None:
        self._store = store
        self._extractor = extractor

        # Session-level caches
        self._user_cache: list[MemoryRecord] | None = None
        self._project_cache: dict[str, list[MemoryRecord]] = {}
        self._domain_cache: list[MemoryRecord] = []
        self._last_domain_query: str = ""
        self._dirty = False  # set True after extract_and_store → invalidate caches

    # ── LangGraph node: inject ───────────────────────────────────────────

    async def inject(self, state: AgentState) -> dict[str, Any]:
        """Load relevant memories and return them as an XML context block."""
        try:
            project_id = state.get("project_id") or ""

            if self._dirty:
                self._user_cache = None
                self._project_cache.pop(project_id, None)
                self._dirty = False

            if self._user_cache is None:
                self._user_cache = await self._list_tier("user", scope=None)
            user_memories = self._user_cache

            if project_id not in self._project_cache:
                self._project_cache[project_id] = await self._list_tier(
                    "project", scope=project_id
                )
            project_memories = self._project_cache[project_id]

            messages = state.get("messages") or []
            last_user_text = self._last_user_text(messages)
            domain_memories = await self._get_domain_cached(last_user_text)

            block = self._build_xml(user_memories, project_memories, domain_memories)
            log.info(
                "memory_middleware.injected",
                user=len(user_memories),
                project=len(project_memories),
                domain=len(domain_memories),
                cache_hit=last_user_text == self._last_domain_query,
            )
            return {"memory_context": block}
        except Exception:
            log.exception("memory_middleware.inject_failed")
            return {"memory_context": ""}

    async def _list_tier(self, tier: str, scope: str | None) -> list[MemoryRecord]:
        """Enumerate every entry in a tier/scope.

        The library's ``MemoryStore`` protocol exposes ``search`` (FTS-backed)
        and ``read`` (by key) but deliberately omits an unfiltered list —
        other consumers (apt-legal, prime-jennie) do not need it. ax's 3-layer
        injector does need it, so we reach into the concrete SQLite
        connection when the store is a ``SqliteMemoryStore``; otherwise we
        return empty (the domain/search path still works).
        """
        import asyncio

        conn = getattr(self._store, "_conn", None)
        if conn is None:
            return []

        if scope is None:
            sql = "SELECT * FROM memories WHERE tier = ? ORDER BY updated_at DESC LIMIT 200"
            params: tuple[Any, ...] = (tier,)
        else:
            sql = (
                "SELECT * FROM memories WHERE tier = ? AND scope = ? "
                "ORDER BY updated_at DESC LIMIT 200"
            )
            params = (tier, scope)

        try:
            rows = await asyncio.to_thread(
                lambda: conn.execute(sql, params).fetchall()
            )
        except Exception:
            log.exception("memory_middleware.list_tier_failed", tier=tier, scope=scope)
            return []

        records: list[MemoryRecord] = []
        from minyoung_mah.memory.store import _row_to_entry  # type: ignore[attr-defined]

        for row in rows:
            try:
                records.append(entry_to_record(_row_to_entry(row)))
            except Exception:
                continue
        return records

    async def _get_domain_cached(self, query: str) -> list[MemoryRecord]:
        if not query:
            return self._domain_cache

        if self._last_domain_query:
            similarity = SequenceMatcher(
                None, self._last_domain_query[:200], query[:200]
            ).ratio()
            if similarity >= _TOPIC_SIMILARITY_THRESHOLD:
                return self._domain_cache

        try:
            entries = await self._store.search(
                tier="domain", query=query, scope=None, limit=10
            )
        except Exception:
            log.exception("memory_middleware.domain_search_failed")
            entries = []
        self._domain_cache = [entry_to_record(e) for e in entries]
        self._last_domain_query = query
        return self._domain_cache

    # ── LangGraph node: extract_and_store ────────────────────────────────

    async def extract_and_store(self, state: AgentState) -> dict[str, Any]:
        """Extract durable facts and persist them via the library store."""
        try:
            messages = state.get("messages") or []
            if not messages:
                log.debug("memory_middleware.extract_skip_no_messages")
                return {}

            existing_keys = self._collect_cached_keys()
            project_id = state.get("project_id")

            # Extractor is sync (LLM-based); offload to a thread so we don't
            # block the event loop.
            new_records = await asyncio.to_thread(
                self._extractor.extract, messages, existing_keys
            )

            for record in new_records:
                if record.layer == "project" and project_id:
                    record.project_id = project_id
                entry = record_to_entry(record)
                await self._store.write(
                    tier=entry.tier,
                    key=entry.key,
                    value=entry.value,
                    scope=entry.scope,
                    metadata=entry.metadata,
                )

            if new_records:
                self._dirty = True
            log.info("memory_middleware.extracted_and_stored", count=len(new_records))
            return {}
        except Exception:
            log.exception("memory_middleware.extract_and_store_failed")
            return {}

    def _collect_cached_keys(self) -> set[str]:
        """Keys the extractor should avoid duplicating.

        Sourced from the 3 tier caches — the library store has no
        "list all keys" helper, but the caches already hold everything the
        current session has observed, which is what the extractor hint
        actually needs (prevent duplicate writes *this session*).
        """
        keys: set[str] = set()
        if self._user_cache:
            keys.update(r.key for r in self._user_cache)
        for recs in self._project_cache.values():
            keys.update(r.key for r in recs)
        keys.update(r.key for r in self._domain_cache)
        return keys

    # ── Internal helpers ─────────────────────────────────────────────────

    @staticmethod
    def _last_user_text(messages: list) -> str:
        for msg in reversed(messages):
            if hasattr(msg, "type") and msg.type == "human":
                return msg.content if isinstance(msg.content, str) else str(msg.content)
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content", "")
                return content if isinstance(content, str) else str(content)
        return ""

    @staticmethod
    def _build_xml(
        user: list[MemoryRecord],
        project: list[MemoryRecord],
        domain: list[MemoryRecord],
    ) -> str:
        parts = [
            "<agent_memory>",
            "<user>",
            _format_records(user),
            "</user>",
            "<project>",
            _format_records(project),
            "</project>",
            "<domain>",
            _format_records(domain),
            "</domain>",
            "</agent_memory>",
        ]
        return "\n".join(parts)
