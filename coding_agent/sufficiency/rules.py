"""rule_gate — signals dict 을 HIGH/MEDIUM/LOW 로 분류하고, LOW 일 때
LLM critic 호출 없이 휴리스틱 verdict 를 만든다.

apt-legal 의 ``rules.evaluate_sufficiency`` + ``_route_low_to_verdict``
패턴을 ax 코딩 도메인 신호에 맞게 재구성:

- HIGH: pytest pass + lint 0 + todo_ratio 충분 + PRD 커버리지 충분
- LOW : pytest fail / todo_ratio 매우 낮음 / PRD 커버리지 매우 낮음
- MEDIUM: 그 외 — LLM critic 으로 판정

None 신호는 "신호 없음" 으로 다뤄 HIGH 쪽을 막지 않는다 (보수).
"""

from __future__ import annotations

from typing import Any

from coding_agent.sufficiency.schemas import (
    CodeQualityGateResult,
    CriticVerdict,
    GateLevel,
)


def evaluate(
    signals: dict[str, Any],
    *,
    high_todo: float,
    low_todo: float,
    high_prd: float,
    low_prd: float,
) -> CodeQualityGateResult:
    """Classify signals into HIGH/MEDIUM/LOW.

    None 가 들어온 신호는 분기 결정에 영향을 주지 않도록 처리:
    - ``pytest_exit is None`` : 테스트 미실행 — HIGH 막지 않음, LOW 도 안 만듦
    - ``lint_errors is None`` : reviewer 미동작 — HIGH 막지 않음
    - ``todo_total == 0``     : ledger 미사용 — todo_ratio 는 1.0 으로 간주
    - ``prd_coverage`` 가 1.0 : PRD 부재 — HIGH 막지 않음
    """
    pytest_exit = signals.get("pytest_exit")
    lint_errors = signals.get("lint_errors")
    todo_ratio = float(signals.get("todo_ratio", 1.0))
    prd_coverage = float(signals.get("prd_coverage", 1.0))

    triggered: list[str] = []
    reasons: list[str] = []

    # ── LOW ──
    if pytest_exit is not None and pytest_exit != 0:
        triggered.append(f"pytest_exit={pytest_exit}")
        reasons.append(f"pytest 실패 (exit={pytest_exit})")
        return CodeQualityGateResult(
            level="LOW",
            triggered_signals=triggered,
            metrics=dict(signals),
            reason="; ".join(reasons),
        )
    if todo_ratio < low_todo:
        triggered.append(f"todo_ratio={todo_ratio:.2f}<{low_todo}")
        reasons.append(f"todo 완료율 {todo_ratio:.0%} < {low_todo:.0%}")
        return CodeQualityGateResult(
            level="LOW",
            triggered_signals=triggered,
            metrics=dict(signals),
            reason="; ".join(reasons),
        )
    if prd_coverage < low_prd:
        triggered.append(f"prd_coverage={prd_coverage:.2f}<{low_prd}")
        reasons.append(f"PRD 커버리지 {prd_coverage:.0%} < {low_prd:.0%}")
        return CodeQualityGateResult(
            level="LOW",
            triggered_signals=triggered,
            metrics=dict(signals),
            reason="; ".join(reasons),
        )

    # ── HIGH ──
    high_pytest = pytest_exit in (0, None)
    high_lint = lint_errors is None or lint_errors == 0
    high_todo_ok = todo_ratio >= high_todo
    high_prd_ok = prd_coverage >= high_prd
    if high_pytest and high_lint and high_todo_ok and high_prd_ok:
        if pytest_exit == 0:
            triggered.append("pytest_pass")
        if lint_errors == 0:
            triggered.append("lint_zero")
        triggered.append(f"todo_ratio={todo_ratio:.2f}>={high_todo}")
        triggered.append(f"prd_coverage={prd_coverage:.2f}>={high_prd}")
        return CodeQualityGateResult(
            level="HIGH",
            triggered_signals=triggered,
            metrics=dict(signals),
            reason="모든 신호가 임계값 통과",
        )

    # ── MEDIUM ──
    if not high_todo_ok:
        triggered.append(f"todo_ratio={todo_ratio:.2f}")
    if not high_prd_ok:
        triggered.append(f"prd_coverage={prd_coverage:.2f}")
    if not high_pytest:
        triggered.append(f"pytest_exit={pytest_exit}")
    if not high_lint:
        triggered.append(f"lint_errors={lint_errors}")
    return CodeQualityGateResult(
        level="MEDIUM",
        triggered_signals=triggered or ["보더라인"],
        metrics=dict(signals),
        reason="HIGH 임계값 일부 미달 — LLM critic 판정 필요",
    )


def heuristic_verdict_for_low(
    gate: CodeQualityGateResult,
) -> CriticVerdict:
    """LOW band 에서 LLM critic 비용 없이 retry/replan 결정을 만든다.

    apt-legal `_route_low_to_verdict` 의 ax 도메인 적용:
    - pytest 실패 → ``target_role="fixer"`` retry
    - todo_ratio 부족 → ``target_role="coder"`` retry
    - PRD 커버리지만 낮음 → ``target_role="planner"`` replan

    어떤 분기든 작은 자연어 feedback 을 ``feedback_for_retry`` 에 채워
    다음 iteration 에 HumanMessage 로 주입된다.
    """
    metrics = gate.metrics
    pytest_exit = metrics.get("pytest_exit")
    todo_ratio = float(metrics.get("todo_ratio", 1.0))
    prd_coverage = float(metrics.get("prd_coverage", 1.0))

    if pytest_exit is not None and pytest_exit != 0:
        return CriticVerdict(
            verdict="retry_lookup",
            target_role="fixer",
            reason=(
                f"결정론 게이트 LOW — pytest 실패 (exit={pytest_exit}). "
                f"fixer 위임으로 결함 해소 필요."
            ),
            feedback_for_retry=(
                "이전 시도에서 verifier 의 pytest 가 실패했다. 실패 원인을 "
                "고정하기 위해 fixer 에게 위임하고, 그 후 verifier 를 다시 "
                "돌려라. 실패 메시지와 stack trace 를 fixer 의 task 설명에 "
                "구체적으로 옮길 것."
            ),
        )

    if todo_ratio < 0.5:
        return CriticVerdict(
            verdict="retry_lookup",
            target_role="coder",
            reason=(
                f"결정론 게이트 LOW — todo 완료율 {todo_ratio:.0%}. "
                f"미완료 task 를 coder 에게 추가 위임해야 한다."
            ),
            feedback_for_retry=(
                "ledger 의 pending/in_progress task 가 아직 절반 이상 남았다. "
                "남은 task 를 coder 에게 위임해 완료시킨 뒤 다시 종료를 "
                "시도하라. 지금 자연어 요약으로 종료하지 마라."
            ),
        )

    # PRD coverage 만 낮은 케이스 → 분해를 다시 봐야 한다
    return CriticVerdict(
        verdict="replan",
        target_role="planner",
        reason=(
            f"결정론 게이트 LOW — PRD 커버리지 {prd_coverage:.0%}. "
            f"분해가 사용자 요청의 일부를 빠뜨린 것으로 보인다."
        ),
        feedback_for_retry=(
            "산출물 텍스트에서 사용자 요청의 핵심 명사구가 충분히 다뤄지지 "
            "않았다. planner 를 다시 호출해 누락된 영역을 식별하고 보완 "
            "task 를 분해해 ledger 에 등록한 뒤, 사용자에게 변경 사항을 "
            "확인받고 진행하라."
        ),
    )


def gate_level_to_label(level: GateLevel) -> str:
    """User-facing 표시용 라벨 (Rich panel / 로그)."""
    return {"HIGH": "충분", "MEDIUM": "보더라인", "LOW": "부족"}.get(level, level)


__all__ = [
    "evaluate",
    "heuristic_verdict_for_low",
    "gate_level_to_label",
]
