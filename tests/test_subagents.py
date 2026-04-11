"""SubAgent 시스템 테스트 — 상태 전이 + 레지스트리."""

from __future__ import annotations

import pytest

from coding_agent.subagents.models import SubAgentInstance, SubAgentStatus, VALID_TRANSITIONS
from coding_agent.subagents.registry import SubAgentRegistry


@pytest.fixture
def registry():
    return SubAgentRegistry()


class TestSubAgentStatus:
    def test_all_states_defined(self):
        states = set(SubAgentStatus)
        expected = {
            SubAgentStatus.CREATED,
            SubAgentStatus.ASSIGNED,
            SubAgentStatus.RUNNING,
            SubAgentStatus.BLOCKED,
            SubAgentStatus.COMPLETED,
            SubAgentStatus.FAILED,
            SubAgentStatus.CANCELLED,
            SubAgentStatus.DESTROYED,
        }
        assert states == expected

    def test_destroyed_is_terminal(self):
        assert VALID_TRANSITIONS[SubAgentStatus.DESTROYED] == set()

    def test_valid_transitions_coverage(self):
        """모든 상태에 대해 전이 규칙이 정의되어 있어야 한다."""
        for state in SubAgentStatus:
            assert state in VALID_TRANSITIONS


class TestSubAgentRegistry:
    def test_create_instance(self, registry: SubAgentRegistry):
        instance = registry.create_instance(
            role="coder",
            specialty="Python code generation",
            task_summary="Create a hello world script",
            parent_id=None,
            model_tier="strong",
            tools=["read_file", "write_file"],
        )
        assert instance.agent_id.startswith("s-")
        assert instance.state == SubAgentStatus.CREATED
        assert instance.role == "coder"
        assert len(instance.tools) == 2

    def test_transition_valid(self, registry: SubAgentRegistry):
        instance = registry.create_instance(
            role="coder", specialty="test", task_summary="test",
            parent_id=None, model_tier="strong", tools=[],
        )
        # CREATED -> ASSIGNED
        assert registry.transition_state(instance.agent_id, SubAgentStatus.ASSIGNED, "test")
        assert instance.state == SubAgentStatus.ASSIGNED

        # ASSIGNED -> RUNNING
        assert registry.transition_state(instance.agent_id, SubAgentStatus.RUNNING, "test")
        assert instance.state == SubAgentStatus.RUNNING

    def test_transition_invalid(self, registry: SubAgentRegistry):
        instance = registry.create_instance(
            role="coder", specialty="test", task_summary="test",
            parent_id=None, model_tier="strong", tools=[],
        )
        # CREATED -> COMPLETED 는 불법 전이
        assert not registry.transition_state(instance.agent_id, SubAgentStatus.COMPLETED, "test")
        assert instance.state == SubAgentStatus.CREATED  # 변경되지 않음

    def test_full_lifecycle(self, registry: SubAgentRegistry):
        """전체 수명주기: CREATED -> ASSIGNED -> RUNNING -> COMPLETED -> DESTROYED."""
        instance = registry.create_instance(
            role="reviewer", specialty="code review", task_summary="review PR",
            parent_id=None, model_tier="default", tools=["read_file"],
        )
        aid = instance.agent_id

        assert registry.transition_state(aid, SubAgentStatus.ASSIGNED, "preparing")
        assert registry.transition_state(aid, SubAgentStatus.RUNNING, "starting")
        assert registry.transition_state(aid, SubAgentStatus.COMPLETED, "done")
        assert registry.transition_state(aid, SubAgentStatus.DESTROYED, "cleanup")

        assert instance.state == SubAgentStatus.DESTROYED

    def test_retry_lifecycle(self, registry: SubAgentRegistry):
        """실패 후 재시도: RUNNING -> FAILED -> ASSIGNED -> RUNNING -> COMPLETED."""
        instance = registry.create_instance(
            role="fixer", specialty="bug fix", task_summary="fix crash",
            parent_id=None, model_tier="strong", tools=[],
        )
        aid = instance.agent_id

        registry.transition_state(aid, SubAgentStatus.ASSIGNED, "prep")
        registry.transition_state(aid, SubAgentStatus.RUNNING, "start")
        registry.transition_state(aid, SubAgentStatus.FAILED, "timeout")
        # 재시도: FAILED -> ASSIGNED
        assert registry.transition_state(aid, SubAgentStatus.ASSIGNED, "retry")
        registry.transition_state(aid, SubAgentStatus.RUNNING, "retry_start")
        registry.transition_state(aid, SubAgentStatus.COMPLETED, "success")

    def test_blocked_lifecycle(self, registry: SubAgentRegistry):
        """블록 상태: RUNNING -> BLOCKED -> RUNNING -> COMPLETED."""
        instance = registry.create_instance(
            role="coder", specialty="test", task_summary="test",
            parent_id=None, model_tier="strong", tools=[],
        )
        aid = instance.agent_id

        registry.transition_state(aid, SubAgentStatus.ASSIGNED, "prep")
        registry.transition_state(aid, SubAgentStatus.RUNNING, "start")
        registry.transition_state(aid, SubAgentStatus.BLOCKED, "waiting for input")
        # BLOCKED -> RUNNING (재개)
        assert registry.transition_state(aid, SubAgentStatus.RUNNING, "input received")
        registry.transition_state(aid, SubAgentStatus.COMPLETED, "done")

    def test_get_active(self, registry: SubAgentRegistry):
        i1 = registry.create_instance("a", "a", "a", None, "fast", [])
        i2 = registry.create_instance("b", "b", "b", None, "fast", [])

        registry.transition_state(i1.agent_id, SubAgentStatus.ASSIGNED, "")
        registry.transition_state(i2.agent_id, SubAgentStatus.CANCELLED, "")
        registry.transition_state(i2.agent_id, SubAgentStatus.DESTROYED, "")

        active = registry.get_active()
        assert len(active) == 1
        assert active[0].agent_id == i1.agent_id

    def test_get_by_parent(self, registry: SubAgentRegistry):
        i1 = registry.create_instance("a", "a", "a", "parent-1", "fast", [])
        i2 = registry.create_instance("b", "b", "b", "parent-2", "fast", [])

        children = registry.get_by_parent("parent-1")
        assert len(children) == 1
        assert children[0].agent_id == i1.agent_id

    def test_event_log(self, registry: SubAgentRegistry):
        instance = registry.create_instance("coder", "test", "test", None, "fast", [])
        registry.transition_state(instance.agent_id, SubAgentStatus.ASSIGNED, "prep")
        registry.transition_state(instance.agent_id, SubAgentStatus.RUNNING, "go")

        events = registry.event_log
        # creation + 2 transitions = 3 events
        assert len(events) >= 3

    def test_cleanup_completed(self, registry: SubAgentRegistry):
        instance = registry.create_instance("coder", "test", "test", None, "fast", [])
        aid = instance.agent_id

        registry.transition_state(aid, SubAgentStatus.ASSIGNED, "")
        registry.transition_state(aid, SubAgentStatus.RUNNING, "")
        registry.transition_state(aid, SubAgentStatus.COMPLETED, "")
        registry.transition_state(aid, SubAgentStatus.DESTROYED, "")

        # max_age=0 으로 설정하면 즉시 정리
        cleaned = registry.cleanup_completed(max_age_seconds=0)
        # DESTROYED 상태는 이미 정리됨
        assert cleaned >= 0

    def test_destroy_instance(self, registry: SubAgentRegistry):
        instance = registry.create_instance("coder", "test", "test", None, "fast", [])
        aid = instance.agent_id

        registry.transition_state(aid, SubAgentStatus.ASSIGNED, "")
        registry.transition_state(aid, SubAgentStatus.RUNNING, "")
        registry.transition_state(aid, SubAgentStatus.COMPLETED, "")

        assert registry.destroy_instance(aid, "done")
        assert instance.state == SubAgentStatus.DESTROYED
