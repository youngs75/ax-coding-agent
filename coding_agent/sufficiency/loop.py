"""Sufficiency outer-loop primitives — node-level helpers consumed by
``coding_agent/core/loop.py`` LangGraph nodes.

This module owns the *pure* state machine pieces of the sufficiency
loop so the LangGraph nodes stay thin wrappers. Three responsibilities:

1. **iteration / cycle hash / history** — given the existing AgentState
   ``sufficiency_history`` plus the new ``CriticVerdict`` for this iter,
   decide whether we hit a cycle (verdict-shape repeats) and produce
   the next ``SufficiencyHistoryEntry``.
2. **observer.emit / hitl.notify** — issue the standard 0.1.8 events
   (``orchestrator.critic.verdict``, ``HITLEvent kind="critic_escalate"``).
3. **AgentState mutation helpers** — build the partial-state dict the
   LangGraph nodes return.

LangGraph 노드 자체는 ``coding_agent/core/loop.py`` 의 ``_build_graph``
안에서 정의된다 (closure 로 ``self._orchestrator`` / ``self._todo_store``
등을 잡아야 하므로 모듈로 분리하기 어렵다).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog

from coding_agent.sufficiency.schemas import (
    CodeQualityGateResult,
    CriticVerdict,
    SufficiencyHistoryEntry,
)

if TYPE_CHECKING:
    from minyoung_mah import HITLChannel, Observer

log = structlog.get_logger("sufficiency.loop")


def compute_cycle_hash(
    rule_level: str,
    verdict: str,
    target_role: str | None,
) -> str:
    """Stable 8-char hash for a (level, verdict, target) triple.

    cycle 감지에 사용 — 직전 두 entry 와 동일한 hash 가 또 나오면
    rule_gate ↔ critic 이 같은 결정을 무한 반복하는 상태로 간주.
    """
    raw = f"{rule_level}|{verdict}|{target_role or '_'}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def detect_cycle(
    history: list[dict[str, Any]],
    new_hash: str,
    *,
    window: int = 2,
) -> bool:
    """True if any of the last ``window`` history entries share ``new_hash``.

    - 0~1 entries → no cycle yet (need at least one prior occurrence).
    - 2+ entries → cycle if any of the last ``window`` matches.
    """
    if not history:
        return False
    for entry in history[-window:]:
        if entry.get("cycle_hash") == new_hash:
            return True
    return False


def build_history_entry(
    iteration: int,
    gate: CodeQualityGateResult,
    verdict: CriticVerdict,
) -> SufficiencyHistoryEntry:
    return SufficiencyHistoryEntry(
        iteration=iteration,
        rule_level=gate.level,
        verdict=verdict.verdict,
        target_role=verdict.target_role,
        cycle_hash=compute_cycle_hash(gate.level, verdict.verdict, verdict.target_role),
    )


def serialize_history_entry(entry: SufficiencyHistoryEntry) -> dict[str, Any]:
    return {
        "iteration": entry.iteration,
        "rule_level": entry.rule_level,
        "verdict": entry.verdict,
        "target_role": entry.target_role,
        "cycle_hash": entry.cycle_hash,
    }


def serialize_verdict(verdict: CriticVerdict) -> dict[str, Any]:
    return {
        "verdict": verdict.verdict,
        "target_role": verdict.target_role,
        "reason": verdict.reason,
        "feedback_for_retry": verdict.feedback_for_retry,
    }


def force_escalate_if_blocked(
    verdict: CriticVerdict,
    *,
    iteration: int,
    max_iterations: int,
    is_cycle: bool,
) -> CriticVerdict:
    """Promote a retry/replan verdict to ``escalate_hitl`` when the loop
    is exhausted (max iterations reached) or a cycle is detected.

    apt-legal `loop.py` 의 cycle/MAX_ITERATIONS 강제 escalation 패턴.
    pass / 이미 escalate 인 경우는 그대로 통과.

    ``max_iterations`` 의미: "최대 N 회 retry 시도 후 escalate". 즉 N=1
    이면 첫 진입 (iteration=1) 의 retry 는 통과하고, 그 retry 후 두 번째
    진입 (iteration=2) 에서 escalate. 0 은 retry 자체를 허용하지 않음
    (즉시 escalate). 비교는 ``iteration > max_iterations`` — ``>=`` 로
    두면 N=1 이 "1 회 진입 시 즉시 escalate" 가 되어 의도와 어긋난다.
    """
    if verdict.verdict in ("pass", "escalate_hitl"):
        return verdict
    if is_cycle:
        return CriticVerdict(
            verdict="escalate_hitl",
            target_role=None,
            reason=(
                f"sufficiency loop 사이클 감지 (iteration={iteration}). "
                f"동일한 (level, verdict, target) 결정이 반복됨 — 사용자 검토 필요. "
                f"원본 사유: {verdict.reason}"
            ),
            feedback_for_retry=None,
        )
    if iteration > max_iterations:
        return CriticVerdict(
            verdict="escalate_hitl",
            target_role=None,
            reason=(
                f"sufficiency loop 최대 반복 도달 ({iteration}/{max_iterations}). "
                f"사용자 검토 필요. 원본 사유: {verdict.reason}"
            ),
            feedback_for_retry=None,
        )
    return verdict


async def emit_critic_verdict_event(
    observer: "Observer",
    *,
    verdict: CriticVerdict,
    iteration: int,
    rule_level: str,
    metrics: dict[str, Any],
) -> None:
    """Fire ``orchestrator.critic.verdict`` for Langfuse / OTel adapters.

    Failures are swallowed — observer 오류가 main flow 를 깨뜨리면 안 됨.
    """
    from minyoung_mah import ObserverEvent

    try:
        await observer.emit(
            ObserverEvent(
                name="orchestrator.critic.verdict",
                timestamp=datetime.now(timezone.utc),
                role="critic",
                metadata={
                    "verdict": verdict.verdict,
                    "target_role": verdict.target_role,
                    "reason": verdict.reason[:500],
                    "iteration": iteration,
                    "rule_level": rule_level,
                    "metrics": metrics,
                },
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("sufficiency.observer.emit_failed", error=str(exc))


async def notify_hitl_escalation(
    hitl: "HITLChannel",
    *,
    verdict: CriticVerdict,
    iteration: int,
    metrics: dict[str, Any],
    answer_preview: str = "",
) -> None:
    """Push ``critic_escalate`` HITLEvent onto the notifications queue.

    apt-legal 패턴: 단방향 — 사람 응답 대기하지 않고 best-effort 답변과
    needs_human_review 플래그를 함께 종료한다.
    """
    from minyoung_mah import HITLEvent

    try:
        await hitl.notify(
            HITLEvent(
                kind="critic_escalate",
                data={
                    "reason": verdict.reason,
                    "iteration": iteration,
                    "metrics": metrics,
                    "answer_preview": answer_preview[:300],
                },
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("sufficiency.hitl.notify_failed", error=str(exc))


def build_feedback_human_message(verdict: CriticVerdict) -> str:
    """Format the feedback text injected as a HumanMessage on retry/replan.

    target_role 힌트를 명시해 시스템 프롬프트가 다음 task() 호출을
    어디로 위임할지 결정하기 쉽게 한다.
    """
    target = verdict.target_role or "(자율 결정)"
    feedback = verdict.feedback_for_retry or "(critic 이 구체 지시를 제공하지 않음)"
    return (
        f"## sufficiency feedback (iteration {{iter}})\n"
        f"sufficiency critic 이 다음 사유로 추가 작업이 필요하다고 판단:\n\n"
        f"- 사유: {verdict.reason}\n"
        f"- 권장 위임 대상: **{target}**\n\n"
        f"### 다음 단계 지시\n{feedback}\n\n"
        f"위 지시에 따라 적절한 SubAgent 에게 task 도구로 위임하라. "
        f"권장 대상이 명시되어 있으면 우선 고려하되, 더 적절한 role 이 "
        f"있다면 자율적으로 선택해도 된다."
    )


__all__ = [
    "build_feedback_human_message",
    "build_history_entry",
    "compute_cycle_hash",
    "detect_cycle",
    "emit_critic_verdict_event",
    "force_escalate_if_blocked",
    "notify_hitl_escalation",
    "serialize_history_entry",
    "serialize_verdict",
]
