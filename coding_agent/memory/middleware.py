"""MemoryMiddleware — LangGraph node functions for memory injection and extraction."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

import structlog

from coding_agent.core.state import AgentState
from coding_agent.memory.extractor import MemoryExtractor
from coding_agent.memory.schema import MemoryRecord
from coding_agent.memory.store import MemoryStore

log = structlog.get_logger(__name__)

_TOPIC_SIMILARITY_THRESHOLD = 0.6  # below this → topic changed, re-search domain


def _format_records(records: list[MemoryRecord]) -> str:
    """Format a list of MemoryRecords as bullet points."""
    if not records:
        return "(none)"
    lines: list[str] = []
    for rec in records:
        lines.append(f"- [{rec.category}] {rec.key}: {rec.content}")
    return "\n".join(lines)


class MemoryMiddleware:
    """Provides LangGraph node functions for injecting and extracting memories.

    Includes session-level caching:
    - user/project memories are cached and only refreshed after extraction
    - domain memories are re-searched only when the user topic changes
    """

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

    def inject(self, state: AgentState) -> dict[str, Any]:
        """Load relevant memories and return them as an XML context block.

        Uses session-level caching to avoid redundant SQLite queries:
        - user/project: cached until new memories are extracted
        - domain: re-searched only when the topic similarity drops below threshold
        """
        try:
            project_id = state.get("project_id") or ""

            # Invalidate caches if new memories were extracted since last inject
            if self._dirty:
                self._user_cache = None
                self._project_cache.pop(project_id, None)
                self._dirty = False

            # User-layer memories (global)
            if self._user_cache is None:
                self._user_cache = self._store.get_by_layer("user")
            user_memories = self._user_cache

            # Project-layer memories (scoped)
            if project_id not in self._project_cache:
                self._project_cache[project_id] = self._store.get_by_layer(
                    "project", project_id=project_id
                )
            project_memories = self._project_cache[project_id]

            # Domain-layer: only re-search when topic changes
            messages = state.get("messages") or []
            last_user_text = self._last_user_text(messages)
            domain_memories = self._get_domain_cached(last_user_text)

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

    def _get_domain_cached(self, query: str) -> list[MemoryRecord]:
        """Return domain memories, re-searching only when topic changes."""
        if not query:
            return self._domain_cache

        # Check topic similarity with the last query
        if self._last_domain_query:
            similarity = SequenceMatcher(
                None, self._last_domain_query[:200], query[:200]
            ).ratio()
            if similarity >= _TOPIC_SIMILARITY_THRESHOLD:
                return self._domain_cache

        # Topic changed — perform a new search
        self._domain_cache = self._store.search(query, layer="domain", limit=10)
        self._last_domain_query = query
        return self._domain_cache

    # ── LangGraph node: extract_and_store ────────────────────────────────

    def extract_and_store(self, state: AgentState) -> dict[str, Any]:
        """Extract durable facts from recent messages and persist them.

        This function is designed to be used as a LangGraph node.  It runs
        the extractor on recent messages, upserts new records into the store,
        and returns an empty dict (no state mutation needed).
        """
        try:
            messages = state.get("messages") or []
            if not messages:
                log.debug("memory_middleware.extract_skip_no_messages")
                return {}

            existing_keys = self._store.get_existing_keys()
            project_id = state.get("project_id")

            new_records = self._extractor.extract(messages, existing_keys)

            for record in new_records:
                # Attach the current project_id for project-layer memories.
                if record.layer == "project" and project_id:
                    record.project_id = project_id
                self._store.upsert(record)

            if new_records:
                self._dirty = True
            log.info("memory_middleware.extracted_and_stored", count=len(new_records))
            return {}
        except Exception:
            log.exception("memory_middleware.extract_and_store_failed")
            return {}

    # ── Internal helpers ─────────────────────────────────────────────────

    @staticmethod
    def _last_user_text(messages: list) -> str:
        """Extract the text content of the last HumanMessage in the list."""
        for msg in reversed(messages):
            # Support both LangChain message objects and plain dicts.
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
        """Assemble the three memory layers into an XML block."""
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
