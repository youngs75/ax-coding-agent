"""SubAgentRegistry — central registry tracking all SubAgent instances and events."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog

from coding_agent.subagents.models import (
    SubAgentEvent,
    SubAgentInstance,
    SubAgentStatus,
    VALID_TRANSITIONS,
)

log = structlog.get_logger(__name__)


class SubAgentRegistry:
    """Thread-safe registry for SubAgent instances and their lifecycle events.

    All state transitions are validated against VALID_TRANSITIONS and recorded
    as immutable SubAgentEvent entries.
    """

    def __init__(self) -> None:
        self._instances: dict[str, SubAgentInstance] = {}
        self._events: list[SubAgentEvent] = []

    # ── Creation ──────────────────────────────────────────────

    def create_instance(
        self,
        role: str,
        specialty: str,
        task_summary: str,
        parent_id: str | None,
        model_tier: str,
        tools: list[str],
    ) -> SubAgentInstance:
        """Create a new SubAgent instance, register it, and log a creation event."""
        instance = SubAgentInstance.new(
            role=role,
            specialty=specialty,
            task_summary=task_summary,
            parent_id=parent_id,
            model_tier=model_tier,
            tools=tools,
        )
        self._instances[instance.agent_id] = instance

        # Log a synthetic creation event (no from_state)
        event = SubAgentEvent(
            event_id=str(uuid.uuid4()),
            agent_id=instance.agent_id,
            from_state=SubAgentStatus.CREATED,
            to_state=SubAgentStatus.CREATED,
            reason="instance_created",
            timestamp=instance.created_at,
        )
        self._events.append(event)

        log.info(
            "subagent.created",
            agent_id=instance.agent_id,
            role=role,
            specialty=specialty,
            parent_id=parent_id,
            model_tier=model_tier,
        )
        return instance

    # ── State transitions ─────────────────────────────────────

    def transition_state(
        self,
        agent_id: str,
        new_state: SubAgentStatus,
        reason: str = "",
    ) -> bool:
        """Attempt to transition an agent to *new_state*.

        Returns True on success, False if the transition is invalid or the
        agent does not exist.
        """
        instance = self._instances.get(agent_id)
        if instance is None:
            log.warning("subagent.transition.not_found", agent_id=agent_id)
            return False

        current = instance.state
        allowed = VALID_TRANSITIONS.get(current, set())

        if new_state not in allowed:
            log.warning(
                "subagent.transition.invalid",
                agent_id=agent_id,
                from_state=current.value,
                to_state=new_state.value,
                reason=reason,
            )
            return False

        now = datetime.now(timezone.utc)
        old_state = instance.state
        instance.state = new_state
        instance.updated_at = now

        event = SubAgentEvent(
            event_id=str(uuid.uuid4()),
            agent_id=agent_id,
            from_state=old_state,
            to_state=new_state,
            reason=reason,
            timestamp=now,
        )
        self._events.append(event)

        log.info(
            "subagent.transition",
            agent_id=agent_id,
            from_state=old_state.value,
            to_state=new_state.value,
            reason=reason,
        )
        return True

    # ── Queries ────────────────────────────────────────────────

    def get_instance(self, agent_id: str) -> SubAgentInstance | None:
        """Return the instance for *agent_id*, or None."""
        return self._instances.get(agent_id)

    def get_active(self) -> list[SubAgentInstance]:
        """Return all instances that have not been destroyed."""
        return [
            inst
            for inst in self._instances.values()
            if inst.state != SubAgentStatus.DESTROYED
        ]

    def get_by_parent(self, parent_id: str) -> list[SubAgentInstance]:
        """Return all instances whose parent matches *parent_id*."""
        return [
            inst
            for inst in self._instances.values()
            if inst.parent_id == parent_id
        ]

    # ── Lifecycle helpers ─────────────────────────────────────

    def destroy_instance(self, agent_id: str, reason: str = "cleanup") -> bool:
        """Transition an instance to DESTROYED."""
        return self.transition_state(agent_id, SubAgentStatus.DESTROYED, reason=reason)

    def cleanup_completed(self, max_age_seconds: float = 300) -> int:
        """Destroy completed/failed instances older than *max_age_seconds*.

        Returns the number of instances destroyed.
        """
        now = datetime.now(timezone.utc)
        count = 0
        terminal_states = {SubAgentStatus.COMPLETED, SubAgentStatus.FAILED}

        for inst in list(self._instances.values()):
            if inst.state not in terminal_states:
                continue
            age = (now - inst.updated_at).total_seconds()
            if age >= max_age_seconds:
                if self.destroy_instance(inst.agent_id, reason="cleanup_completed"):
                    count += 1

        if count:
            log.info("subagent.cleanup", destroyed=count)
        return count

    # ── Event log ─────────────────────────────────────────────

    @property
    def event_log(self) -> list[SubAgentEvent]:
        """Return a copy of the full event log."""
        return list(self._events)
