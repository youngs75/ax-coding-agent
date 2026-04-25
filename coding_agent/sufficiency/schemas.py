"""Sufficiency loop 데이터 타입.

apt-legal 의 ``schemas.CriticVerdict`` 4-verdict 구조를 그대로 유지하되,
ax 코딩 도메인 신호(pytest/lint/todo/PRD)를 ``CodeQualityGateResult``
metrics 로 노출한다 (apt-legal 의 RuleGateResult 자리).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

GateLevel = Literal["HIGH", "MEDIUM", "LOW"]
Verdict = Literal["pass", "retry_lookup", "replan", "escalate_hitl"]
FinalVerdict = Literal["pass", "max_iterations", "escalated"]


@dataclass(frozen=True)
class CodeQualityGateResult:
    """Deterministic rule_gate 출력. critic 호출 여부와 LOW 휴리스틱
    verdict 의 입력으로 쓰인다.

    metrics 는 free-form dict — 보통 다음 키를 포함:
    ``pytest_exit, lint_errors, todo_done, todo_total, todo_ratio,
    prd_coverage``. None 값은 "신호 없음" 으로 간주.
    """

    level: GateLevel
    triggered_signals: list[str]  # 어떤 신호가 분기를 결정했는지 (디버깅용)
    metrics: dict[str, Any]
    reason: str


@dataclass(frozen=True)
class CriticVerdict:
    """LLM critic 결정 (또는 LOW 휴리스틱이 만든 결정).

    ``target_role`` 은 retry/replan 시 시스템 프롬프트 텍스트에 노출되는
    힌트. orchestrator 가 그대로 따를지는 자율 — 강제 위임은 하지 않음.
    """

    verdict: Verdict
    target_role: str | None  # "coder" | "verifier" | "fixer" | "planner" | None
    reason: str
    feedback_for_retry: str | None  # 다음 iteration 의 agent 메시지에 주입할 텍스트


@dataclass(frozen=True)
class SufficiencyHistoryEntry:
    """sufficiency loop 의 한 iteration 기록 (cycle 감지 + 결과 노출용)."""

    iteration: int
    rule_level: str
    verdict: str
    target_role: str | None
    cycle_hash: str


@dataclass
class SufficiencyLoopResult:
    """outer loop 의 최종 반환 — CLI / Langfuse 가 표시할 요약."""

    final_verdict: FinalVerdict
    iterations_used: int
    needs_human_review: bool
    review_reason: str | None
    history: list[SufficiencyHistoryEntry] = field(default_factory=list)
    last_metrics: dict[str, Any] = field(default_factory=dict)
