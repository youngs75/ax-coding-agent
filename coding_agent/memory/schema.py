"""MemoryRecord — 3계층 장기 메모리 레코드 스키마."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


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
