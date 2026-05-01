"""rule_gate — signals dict 을 HIGH/MEDIUM/LOW 로 분류하고, LOW 일 때
LLM critic 호출 없이 휴리스틱 verdict 를 만든다.

apt-legal 의 ``rules.evaluate_sufficiency`` + ``_route_low_to_verdict``
패턴을 ax 코딩 도메인 신호에 맞게 재구성:

- HIGH: pytest pass + lint 0 + todo_ratio 충분 + DONE_CONDITION 위반 0
- LOW : pytest fail / todo_ratio 매우 낮음 / DONE_CONDITION 위반 / 산출물 누락
- MEDIUM: 그 외 — LLM critic 으로 판정 (PRD ↔ 산출물 의미 정합성은 critic 영역)

None 신호는 "신호 없음" 으로 다뤄 HIGH 쪽을 막지 않는다 (보수).

R-003 폐기 (2026-05-01): ``prd_coverage`` 신호 + ``high_prd`` / ``low_prd``
임계값 분기 모두 제거. PRD ↔ 산출물 정합성은 substring matching + 임계값
분류로 처리할 수 없는 *판단 영역* — critic LLM 으로 위임.
"""

from __future__ import annotations

from typing import Any

from coding_agent.sufficiency.schemas import (
    CodeQualityGateResult,
    CriticVerdict,
    GateLevel,
)


_ARTIFACT_FILE_HINTS: dict[str, str] = {
    "prd": "PRD.md",
    "spec": "SPEC.md",
    "ledger": "todo ledger 등록 (planner 결과를 ledger SubAgent 에게 위임)",
}


def _artifact_to_file_hint(artifact_id: str) -> str:
    return _ARTIFACT_FILE_HINTS.get(artifact_id, artifact_id)


def evaluate(
    signals: dict[str, Any],
    *,
    high_todo: float,
    low_todo: float,
) -> CodeQualityGateResult:
    """Classify signals into HIGH/MEDIUM/LOW.

    None 가 들어온 신호는 분기 결정에 영향을 주지 않도록 처리:
    - ``pytest_exit is None`` : 테스트 미실행 — HIGH 막지 않음, LOW 도 안 만듦
    - ``lint_errors is None`` : reviewer 미동작 — HIGH 막지 않음
    - ``todo_total == 0``     : ledger 미사용 — todo_ratio 는 1.0 으로 간주
    """
    pytest_exit = signals.get("pytest_exit")
    lint_errors = signals.get("lint_errors")
    todo_ratio = float(signals.get("todo_ratio", 1.0))
    artifacts_missing = list(signals.get("artifacts_missing") or [])
    done_condition_violations = list(signals.get("done_condition_violations") or [])

    triggered: list[str] = []
    reasons: list[str] = []

    # ── LOW: DONE_CONDITION.md 위반 (v22 #3) ──
    # planner 가 작성한 DONE_CONDITION.md 의 forbidden patterns 가 워크스페이스
    # 에 등장 = stack misalignment 등 *기획 위반*. v21 의 React 선택→Vue 작성
    # 회귀 직접 차단. 다른 LOW 신호보다 *최우선* — 잘못된 방향으로 더 진행하지
    # 않게 즉시 fixer 또는 planner 위임.
    if done_condition_violations:
        triggered.append(
            f"done_condition_violations={done_condition_violations[:3]}"
            + ("..." if len(done_condition_violations) > 3 else "")
        )
        reasons.append(
            f"DONE_CONDITION 위반 ({len(done_condition_violations)}건): "
            f"{', '.join(done_condition_violations[:3])}"
        )
        return CodeQualityGateResult(
            level="LOW",
            triggered_signals=triggered,
            metrics=dict(signals),
            reason="; ".join(reasons),
        )

    # ── LOW: 사용자 요청 산출물 누락 (옵션 C) ──
    # SubAgent 가 COMPLETED 라고 주장해도 사용자가 명시한 산출물 (PRD/SPEC/
    # ledger) 이 워크스페이스에 없으면 *기획·분해 단계 미완료* 로 분류.
    # deepseek 처럼 ask 만 하고 종료하는 패턴 (v12 회귀) 의 직접 검출.
    if artifacts_missing:
        triggered.append(f"artifacts_missing={artifacts_missing}")
        reasons.append(
            f"사용자 요청 산출물 누락: {', '.join(artifacts_missing)}"
        )
        return CodeQualityGateResult(
            level="LOW",
            triggered_signals=triggered,
            metrics=dict(signals),
            reason="; ".join(reasons),
        )

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

    # ── HIGH ──
    high_pytest = pytest_exit in (0, None)
    high_lint = lint_errors is None or lint_errors == 0
    high_todo_ok = todo_ratio >= high_todo
    if high_pytest and high_lint and high_todo_ok:
        if pytest_exit == 0:
            triggered.append("pytest_pass")
        if lint_errors == 0:
            triggered.append("lint_zero")
        triggered.append(f"todo_ratio={todo_ratio:.2f}>={high_todo}")
        return CodeQualityGateResult(
            level="HIGH",
            triggered_signals=triggered,
            metrics=dict(signals),
            reason="결정론 신호 모두 임계값 통과 — PRD ↔ 산출물 정합성은 critic 영역",
        )

    # ── MEDIUM ──
    if not high_todo_ok:
        triggered.append(f"todo_ratio={todo_ratio:.2f}")
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

    apt-legal `_route_low_to_verdict` 의 ax 도메인 적용 (결정론 신호만):
    - DONE_CONDITION 위반 → ``fixer``
    - 산출물 누락 → ``planner`` replan
    - pytest 실패 → ``fixer`` retry
    - todo_ratio 부족 → ``coder`` retry

    PRD ↔ 산출물의 의미적 정합성은 LLM critic 의 영역이라 LOW heuristic 에선
    분기하지 않는다 — MEDIUM 에서 critic 이 직접 판정.

    어떤 분기든 작은 자연어 feedback 을 ``feedback_for_retry`` 에 채워
    다음 iteration 에 HumanMessage 로 주입된다.
    """
    metrics = gate.metrics
    pytest_exit = metrics.get("pytest_exit")
    todo_ratio = float(metrics.get("todo_ratio", 1.0))
    artifacts_missing = list(metrics.get("artifacts_missing") or [])
    done_condition_violations = list(metrics.get("done_condition_violations") or [])

    # v22 #3 — DONE_CONDITION 위반은 다른 신호보다 *최우선*. 잘못된 방향으로
    # 더 진행하지 못하게 즉시 fixer 위임 (필요 시 planner replan 도 가능하나
    # 우선 fixer 가 구체적 위반 파일 제거/교체로 시도).
    if done_condition_violations:
        violations_label = "; ".join(done_condition_violations[:5])
        if len(done_condition_violations) > 5:
            violations_label += f"; ... (외 {len(done_condition_violations) - 5}건)"
        return CriticVerdict(
            verdict="retry_lookup",
            target_role="fixer",
            reason=(
                f"DONE_CONDITION 위반 — planner 가 합의한 forbidden patterns 가 "
                f"워크스페이스에 등장 ({len(done_condition_violations)}건). "
                f"기획 위반은 더 진행하기 전에 제거 필요."
            ),
            feedback_for_retry=(
                f"DONE_CONDITION.md 의 forbidden patterns 가 다음 파일에서 "
                f"발견됨: {violations_label}. fixer 에게 위 파일들을 *삭제* 하고 "
                f"DONE_CONDITION 의 framework 선택과 일치하는 형태로 *재작성* "
                f"하도록 위임하라. 예: Vue 컴포넌트가 발견됐고 React 가 선택됐으면, "
                f"동일 기능의 React 컴포넌트로 교체."
            ),
        )

    # 산출물 누락이 가장 강한 신호 — 다른 신호보다 우선 처리.
    if artifacts_missing:
        missing_label = ", ".join(artifacts_missing)
        # ledger 만 빠진 경우는 *분해 단계 누락* — planner 위임으로 ledger
        # 채우기. PRD/SPEC 가 빠진 경우도 동일 (planner 책임).
        return CriticVerdict(
            verdict="replan",
            target_role="planner",
            reason=(
                f"사용자 요청 산출물 누락 ({missing_label}). SubAgent 가 "
                f"COMPLETED 라고 주장했지만 워크스페이스에서 검증 실패 — "
                f"기획/분해 단계가 완료되지 않음."
            ),
            feedback_for_retry=(
                f"사용자 원 요청에 명시된 산출물({missing_label}) 이 워크스페이스에 "
                f"없습니다. planner 에게 다음을 *반드시 파일로 작성*해 위임하세요: "
                f"{', '.join(_artifact_to_file_hint(a) for a in artifacts_missing)}. "
                f"ask_user_question 답변만으론 task 가 끝나지 않으며, 필수 산출물을 "
                f"실제 파일 (write_file 도구로) 만들고, 분해 결과는 ledger 에게 "
                f"위임해 등록까지 완료해야 합니다."
            ),
        )

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

    # 결정론 신호는 모두 통과했지만 LOW 가 발화한 경우 — 위 early return
    # 들이 모든 LOW 케이스를 흡수하므로 사실상 dead path. contract 상 verdict
    # 는 반환해야 하므로 보수적으로 planner replan (사용자가 종료 의사 결정).
    return CriticVerdict(
        verdict="replan",
        target_role="planner",
        reason=(
            "LOW 게이트 발화했으나 결정론 신호 (DONE_CONDITION / artifacts / "
            "pytest / todo) 는 모두 통과 — 분해 재검토 권장."
        ),
        feedback_for_retry=(
            "결정론 신호는 모두 통과했지만 LOW 게이트가 발화했다. "
            "planner 에게 사용자 원 요청 vs 현 산출물을 다시 검토하도록 위임하라."
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
