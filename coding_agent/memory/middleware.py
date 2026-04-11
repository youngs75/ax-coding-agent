"""MemoryMiddleware — LangGraph node functions for memory injection and extraction."""

from __future__ import annotations

from typing import Any

import structlog

from coding_agent.core.state import AgentState
from coding_agent.memory.extractor import MemoryExtractor
from coding_agent.memory.schema import MemoryRecord
from coding_agent.memory.store import MemoryStore

log = structlog.get_logger(__name__)


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

    Usage in graph construction::

        middleware = MemoryMiddleware(store, extractor)
        graph.add_node("inject_memory", middleware.inject)
        graph.add_node("extract_memory", middleware.extract_and_store)
    """

    def __init__(self, store: MemoryStore, extractor: MemoryExtractor) -> None:
        self._store = store
        self._extractor = extractor

    # ── LangGraph node: inject ───────────────────────────────────────────

    def inject(self, state: AgentState) -> dict[str, Any]:
        """Load relevant memories and return them as an XML context block.

        This function is designed to be used as a LangGraph node.  It reads
        user and project memories from the store, searches for domain memories
        relevant to the latest user message, and assembles them into a single
        XML string stored under ``memory_context``.
        """
        try:
            project_id = state.get("project_id")

            # User-layer memories (global — not project-scoped).
            user_memories = self._store.get_by_layer("user")

            # Project-layer memories (scoped to current project when available).
            project_memories = self._store.get_by_layer("project", project_id=project_id)

            # Domain-layer: search for terms relevant to the last user message.
            domain_memories: list[MemoryRecord] = []
            messages = state.get("messages") or []
            last_user_text = self._last_user_text(messages)
            if last_user_text:
                domain_memories = self._store.search(
                    last_user_text, layer="domain", limit=10
                )

            block = self._build_xml(user_memories, project_memories, domain_memories)
            log.info(
                "memory_middleware.injected",
                user=len(user_memories),
                project=len(project_memories),
                domain=len(domain_memories),
            )
            return {"memory_context": block}
        except Exception:
            log.exception("memory_middleware.inject_failed")
            return {"memory_context": ""}

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
