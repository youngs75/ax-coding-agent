"""MemoryRecord — 3계층 장기 메모리 레코드 스키마 + library MemoryEntry 변환."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from minyoung_mah.core.types import MemoryEntry


def _utcnow_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    """Generate a new hex UUID."""
    return uuid.uuid4().hex


@dataclass
class MemoryRecord:
    """A single memory entry in the 3-layer long-term memory system.

    Layers:
        user    — user preferences, habits, coding style
        project — architecture decisions, project rules, conventions
        domain  — business rules, domain terminology, invariants
    """

    layer: Literal["user", "project", "domain"]
    category: str
    key: str
    content: str
    source: str = ""
    project_id: str | None = None
    id: str = field(default_factory=_new_id)
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)

    def touch(self) -> None:
        """Update the ``updated_at`` timestamp to *now*."""
        self.updated_at = _utcnow_iso()


# ---------------------------------------------------------------------------
# Conversion helpers between ax `MemoryRecord` and library `MemoryEntry`.
# See plan Phase 3 — `category + source → metadata{json}`. The library
# schema does not know about `category`/`source`; we carry them through
# metadata so the 3-layer extractor/middleware stay unaware of the shift.
# ---------------------------------------------------------------------------


def record_to_entry(record: MemoryRecord) -> MemoryEntry:
    metadata: dict[str, str] = {"category": record.category}
    if record.source:
        metadata["source"] = record.source
    return MemoryEntry(
        tier=record.layer,
        scope=record.project_id or None,
        key=record.key,
        value=record.content,
        metadata=metadata,
    )


def entry_to_record(entry: MemoryEntry) -> MemoryRecord:
    meta = entry.metadata or {}
    return MemoryRecord(
        layer=entry.tier,  # type: ignore[arg-type]
        category=str(meta.get("category", "")),
        key=entry.key,
        content=entry.value,
        source=str(meta.get("source", "")),
        project_id=entry.scope,
        created_at=(entry.created_at.isoformat() if entry.created_at else ""),
        updated_at=(entry.updated_at.isoformat() if entry.updated_at else ""),
    )
