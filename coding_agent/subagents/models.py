"""SubAgent lifecycle models — status, instances, events, results."""

from __future__ import annotations

import string
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class SubAgentStatus(str, Enum):
    """Lifecycle states for a SubAgent instance."""

    CREATED = "created"
    ASSIGNED = "assigned"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DESTROYED = "destroyed"


VALID_TRANSITIONS: dict[SubAgentStatus, set[SubAgentStatus]] = {
    SubAgentStatus.CREATED: {
        SubAgentStatus.ASSIGNED,
        SubAgentStatus.CANCELLED,
        SubAgentStatus.DESTROYED,
    },
    SubAgentStatus.ASSIGNED: {
        SubAgentStatus.RUNNING,
        SubAgentStatus.CANCELLED,
        SubAgentStatus.DESTROYED,
    },
    SubAgentStatus.RUNNING: {
        SubAgentStatus.COMPLETED,
        SubAgentStatus.FAILED,
        SubAgentStatus.BLOCKED,
        SubAgentStatus.CANCELLED,
    },
    SubAgentStatus.BLOCKED: {
        SubAgentStatus.RUNNING,
        SubAgentStatus.FAILED,
        SubAgentStatus.CANCELLED,
    },
    SubAgentStatus.COMPLETED: {
        SubAgentStatus.DESTROYED,
    },
    SubAgentStatus.FAILED: {
        SubAgentStatus.ASSIGNED,  # retry
        SubAgentStatus.DESTROYED,  # give up
    },
    SubAgentStatus.CANCELLED: {
        SubAgentStatus.DESTROYED,
    },
    SubAgentStatus.DESTROYED: set(),  # terminal
}

_AGENT_ID_CHARS = string.ascii_lowercase + string.digits


def _generate_agent_id() -> str:
    """Generate a unique agent ID with 's-' prefix and 8 random alphanumeric chars."""
    suffix = "".join(random.choices(_AGENT_ID_CHARS, k=8))
    return f"s-{suffix}"


@dataclass
class SubAgentInstance:
    """Runtime representation of a spawned SubAgent."""

    agent_id: str
    role: str
    specialty: str
    task_summary: str
    parent_id: str | None
    state: SubAgentStatus
    model_tier: str
    tools: list[str]
    created_at: datetime
    updated_at: datetime
    result: str | None = None
    error: str | None = None
    retry_count: int = 0
    max_retries: int = 2

    @staticmethod
    def new(
        role: str,
        specialty: str,
        task_summary: str,
        parent_id: str | None,
        model_tier: str,
        tools: list[str],
    ) -> SubAgentInstance:
        """Factory helper — create an instance with generated ID and timestamps."""
        now = datetime.now(timezone.utc)
        return SubAgentInstance(
            agent_id=_generate_agent_id(),
            role=role,
            specialty=specialty,
            task_summary=task_summary,
            parent_id=parent_id,
            state=SubAgentStatus.CREATED,
            model_tier=model_tier,
            tools=list(tools),
            created_at=now,
            updated_at=now,
        )


@dataclass
class SubAgentEvent:
    """Immutable record of a state transition."""

    event_id: str
    agent_id: str
    from_state: SubAgentStatus
    to_state: SubAgentStatus
    reason: str
    timestamp: datetime


@dataclass
class SubAgentResult:
    """Outcome returned after a SubAgent execution completes."""

    success: bool
    output: str
    error: str | None = None
    written_files: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    # If the SubAgent paused on a LangGraph interrupt() (e.g. an
    # ask_user_question call inside a planner), this holds the payload.
    # The caller (task_tool) propagates it to the orchestrator graph by
    # calling interrupt() again. ``thread_id`` lets the caller resume
    # the same SubAgent run later via Command(resume=...).
    interrupt_payload: Any = None
    thread_id: str | None = None
