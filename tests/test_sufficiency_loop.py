"""Sufficiency outer-loop primitives — cycle / max-iter / feedback / events.

LangGraph 노드 자체는 ``test_sufficiency_e2e`` 가 다루고, 여기서는
``coding_agent/sufficiency/loop.py`` 의 순수 함수와 helper 만 검증한다.
"""

from __future__ import annotations

from typing import Any

import pytest

from coding_agent.sufficiency.loop import (
    build_feedback_human_message,
    build_history_entry,
    compute_cycle_hash,
    detect_cycle,
    emit_critic_verdict_event,
    force_escalate_if_blocked,
    notify_hitl_escalation,
    serialize_history_entry,
    serialize_verdict,
)
from coding_agent.sufficiency.schemas import (
    CodeQualityGateResult,
    CriticVerdict,
)


def _verdict(
    v="retry_lookup", target="coder", reason="r", feedback="fb",
):
    return CriticVerdict(
        verdict=v, target_role=target, reason=reason, feedback_for_retry=feedback,
    )


def _gate(level="MEDIUM"):
    return CodeQualityGateResult(
        level=level, triggered_signals=[], metrics={}, reason="r",
    )


# ── cycle hash ──────────────────────────────────────────────────────────────


def test_cycle_hash_stable():
    a = compute_cycle_hash("MEDIUM", "retry_lookup", "coder")
    b = compute_cycle_hash("MEDIUM", "retry_lookup", "coder")
    assert a == b


def test_cycle_hash_distinct():
    a = compute_cycle_hash("MEDIUM", "retry_lookup", "coder")
    b = compute_cycle_hash("MEDIUM", "retry_lookup", "fixer")
    c = compute_cycle_hash("LOW", "retry_lookup", "coder")
    d = compute_cycle_hash("MEDIUM", "replan", "coder")
    assert len({a, b, c, d}) == 4


def test_cycle_hash_handles_none_target():
    h = compute_cycle_hash("MEDIUM", "escalate_hitl", None)
    assert isinstance(h, str) and len(h) == 8


# ── detect_cycle ────────────────────────────────────────────────────────────


def test_detect_cycle_empty_history_no_cycle():
    assert detect_cycle([], "abc12345") is False


def test_detect_cycle_single_entry_match():
    history = [{"cycle_hash": "x"}]
    assert detect_cycle(history, "x") is True


def test_detect_cycle_within_window():
    history = [{"cycle_hash": "a"}, {"cycle_hash": "b"}]
    assert detect_cycle(history, "a") is True   # within window=2


def test_detect_cycle_outside_window():
    history = [
        {"cycle_hash": "a"},
        {"cycle_hash": "b"},
        {"cycle_hash": "c"},
    ]
    # window=2 (default) — only last 2 entries are checked
    assert detect_cycle(history, "a") is False


# ── force_escalate_if_blocked ───────────────────────────────────────────────


def test_force_escalate_passes_through_pass():
    v = _verdict(v="pass", target=None, feedback=None)
    out = force_escalate_if_blocked(v, iteration=5, max_iterations=1, is_cycle=True)
    assert out.verdict == "pass"  # 변경 없음


def test_force_escalate_passes_through_already_escalate():
    v = _verdict(v="escalate_hitl", target=None, feedback=None)
    out = force_escalate_if_blocked(v, iteration=5, max_iterations=1, is_cycle=False)
    assert out.verdict == "escalate_hitl"


def test_force_escalate_on_cycle():
    v = _verdict(v="retry_lookup")
    out = force_escalate_if_blocked(v, iteration=1, max_iterations=3, is_cycle=True)
    assert out.verdict == "escalate_hitl"
    assert "사이클" in out.reason


def test_force_escalate_on_max_iter_reached():
    """MAX_ITER=N 의미: 최대 N 회 retry 후 escalate. N=1 이면 첫 진입은
    retry 통과 (iteration=1), 두 번째 진입에서 (iteration=2) escalate."""
    v = _verdict(v="replan")
    out = force_escalate_if_blocked(v, iteration=2, max_iterations=1, is_cycle=False)
    assert out.verdict == "escalate_hitl"
    assert "최대 반복" in out.reason


def test_force_escalate_max_iter_one_first_attempt_keeps_retry():
    """MAX_ITER=1 + 첫 iteration → 한 번은 retry 시도해야 의미 있는 디자인."""
    v = _verdict(v="retry_lookup")
    out = force_escalate_if_blocked(v, iteration=1, max_iterations=1, is_cycle=False)
    assert out.verdict == "retry_lookup"


def test_force_escalate_max_iter_zero_immediate():
    """MAX_ITER=0 은 'retry 자체를 허용하지 않음' — 즉시 escalate."""
    v = _verdict(v="retry_lookup")
    out = force_escalate_if_blocked(v, iteration=1, max_iterations=0, is_cycle=False)
    assert out.verdict == "escalate_hitl"


def test_force_escalate_under_max_iter_keeps_retry():
    v = _verdict(v="retry_lookup")
    out = force_escalate_if_blocked(v, iteration=1, max_iterations=3, is_cycle=False)
    assert out.verdict == "retry_lookup"  # 보존


# ── history entry / serialize ───────────────────────────────────────────────


def test_build_history_entry_carries_cycle_hash():
    entry = build_history_entry(2, _gate("MEDIUM"), _verdict(v="replan", target="planner"))
    assert entry.iteration == 2
    assert entry.rule_level == "MEDIUM"
    assert entry.verdict == "replan"
    assert entry.target_role == "planner"
    assert entry.cycle_hash == compute_cycle_hash("MEDIUM", "replan", "planner")


def test_serialize_history_entry_keys():
    entry = build_history_entry(1, _gate(), _verdict())
    d = serialize_history_entry(entry)
    assert set(d.keys()) == {
        "iteration", "rule_level", "verdict", "target_role", "cycle_hash"
    }


def test_serialize_verdict_keys():
    d = serialize_verdict(_verdict())
    assert set(d.keys()) == {"verdict", "target_role", "reason", "feedback_for_retry"}


# ── feedback HumanMessage ────────────────────────────────────────────────────


def test_build_feedback_message_includes_target_and_feedback():
    v = _verdict(target="fixer", feedback="pytest 실패 고치고 재실행")
    out = build_feedback_human_message(v).replace("{iter}", "2")
    assert "fixer" in out
    assert "pytest 실패 고치고 재실행" in out
    assert "iteration 2" in out


def test_build_feedback_message_handles_missing_target_and_feedback():
    v = _verdict(target=None, feedback=None)
    out = build_feedback_human_message(v).replace("{iter}", "1")
    assert "자율 결정" in out
    assert "구체 지시를 제공하지 않음" in out


# ── observer.emit / hitl.notify (smoke; failures swallowed) ─────────────────


class _RecorderObserver:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event):  # noqa: ANN001
        self.events.append(event)


class _FailingObserver:
    async def emit(self, event):  # noqa: ANN001
        raise RuntimeError("backend down")


class _RecorderHITL:
    def __init__(self) -> None:
        self.notifications: list[Any] = []

    async def notify(self, event):  # noqa: ANN001
        self.notifications.append(event)


class _FailingHITL:
    async def notify(self, event):  # noqa: ANN001
        raise RuntimeError("queue down")


@pytest.mark.asyncio
async def test_emit_critic_verdict_event_records_metadata():
    obs = _RecorderObserver()
    v = _verdict(v="pass", target=None, feedback=None)
    await emit_critic_verdict_event(
        obs, verdict=v, iteration=3, rule_level="HIGH",
        metrics={"pytest_exit": 0},
    )
    assert len(obs.events) == 1
    e = obs.events[0]
    assert e.name == "orchestrator.critic.verdict"
    assert e.role == "critic"
    assert e.metadata["verdict"] == "pass"
    assert e.metadata["iteration"] == 3
    assert e.metadata["rule_level"] == "HIGH"


@pytest.mark.asyncio
async def test_emit_critic_verdict_event_swallows_observer_failures():
    obs = _FailingObserver()
    v = _verdict(v="pass", target=None, feedback=None)
    # 예외가 누설되면 main flow 가 깨진다 — 통과해야 함
    await emit_critic_verdict_event(
        obs, verdict=v, iteration=1, rule_level="HIGH", metrics={},
    )


@pytest.mark.asyncio
async def test_notify_hitl_escalation_pushes_event():
    h = _RecorderHITL()
    v = _verdict(v="escalate_hitl", target=None, feedback=None)
    await notify_hitl_escalation(
        h, verdict=v, iteration=1, metrics={"pytest_exit": 0},
        answer_preview="answer",
    )
    assert len(h.notifications) == 1
    e = h.notifications[0]
    assert e.kind == "critic_escalate"
    assert e.data["iteration"] == 1
    assert e.data["metrics"]["pytest_exit"] == 0


@pytest.mark.asyncio
async def test_notify_hitl_escalation_swallows_failure():
    h = _FailingHITL()
    v = _verdict(v="escalate_hitl", target=None, feedback=None)
    await notify_hitl_escalation(
        h, verdict=v, iteration=1, metrics={},
    )
