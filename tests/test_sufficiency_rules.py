"""Sufficiency rule_gate — HIGH/MEDIUM/LOW 분류 + LOW 휴리스틱 verdict.

순수 함수 단위 테스트. 임계값 매트릭스의 각 분기 경계와 None 신호의
"보수적 처리" 의도를 함께 검증한다.
"""

from __future__ import annotations

import pytest

from coding_agent.sufficiency.rules import (
    evaluate,
    gate_level_to_label,
    heuristic_verdict_for_low,
)


_DEFAULTS = dict(high_todo=0.9, low_todo=0.5, high_prd=0.85, low_prd=0.4)


def _signals(**overrides):
    base = {
        "pytest_exit": 0,
        "lint_errors": 0,
        "todo_done": 9,
        "todo_total": 10,
        "todo_ratio": 0.9,
        "prd_coverage": 0.9,
        "artifact_intent": [],
        "artifacts_missing": [],
    }
    base.update(overrides)
    return base


# ── HIGH ────────────────────────────────────────────────────────────────────


def test_high_when_all_signals_good():
    g = evaluate(_signals(), **_DEFAULTS)
    assert g.level == "HIGH"
    assert "pytest_pass" in g.triggered_signals
    assert "lint_zero" in g.triggered_signals


def test_high_when_pytest_and_lint_signals_missing():
    """None pytest/lint must NOT block HIGH — '신호 없음' 은 보수적으로 통과."""
    g = evaluate(_signals(pytest_exit=None, lint_errors=None), **_DEFAULTS)
    assert g.level == "HIGH"


def test_high_when_prd_and_todo_at_threshold():
    g = evaluate(_signals(todo_ratio=0.9, prd_coverage=0.85), **_DEFAULTS)
    assert g.level == "HIGH"


# ── LOW ─────────────────────────────────────────────────────────────────────


def test_low_when_pytest_failed():
    g = evaluate(_signals(pytest_exit=1), **_DEFAULTS)
    assert g.level == "LOW"
    assert any("pytest_exit=1" in t for t in g.triggered_signals)


def test_low_when_pytest_timeout():
    g = evaluate(_signals(pytest_exit=-1), **_DEFAULTS)
    assert g.level == "LOW"


def test_low_when_todo_ratio_below_threshold():
    g = evaluate(_signals(todo_ratio=0.49), **_DEFAULTS)
    assert g.level == "LOW"


def test_low_when_prd_coverage_below_threshold():
    g = evaluate(_signals(prd_coverage=0.39), **_DEFAULTS)
    assert g.level == "LOW"


# ── MEDIUM (LLM critic 필요) ────────────────────────────────────────────────


def test_medium_when_todo_ratio_between_thresholds():
    g = evaluate(_signals(todo_ratio=0.7), **_DEFAULTS)
    assert g.level == "MEDIUM"


def test_medium_when_prd_coverage_between_thresholds():
    g = evaluate(_signals(prd_coverage=0.6), **_DEFAULTS)
    assert g.level == "MEDIUM"


def test_medium_when_lint_errors_nonzero_but_pytest_ok():
    g = evaluate(_signals(lint_errors=3), **_DEFAULTS)
    assert g.level == "MEDIUM"  # lint 만으론 LOW 안 만듦


def test_medium_when_only_lint_signal_missing_and_others_borderline():
    g = evaluate(
        _signals(lint_errors=None, todo_ratio=0.6, prd_coverage=0.7),
        **_DEFAULTS,
    )
    assert g.level == "MEDIUM"


# ── LOW heuristic verdict ───────────────────────────────────────────────────


def test_heuristic_pytest_fail_routes_to_fixer():
    g = evaluate(_signals(pytest_exit=1), **_DEFAULTS)
    v = heuristic_verdict_for_low(g)
    assert v.verdict == "retry_lookup"
    assert v.target_role == "fixer"
    assert v.feedback_for_retry  # non-empty
    assert "fixer" in v.reason.lower() or "pytest" in v.reason.lower()


def test_heuristic_low_todo_routes_to_coder():
    g = evaluate(_signals(todo_ratio=0.3, pytest_exit=0), **_DEFAULTS)
    v = heuristic_verdict_for_low(g)
    assert v.verdict == "retry_lookup"
    assert v.target_role == "coder"


def test_heuristic_low_prd_only_routes_to_planner_replan():
    g = evaluate(
        _signals(prd_coverage=0.3, pytest_exit=0, todo_ratio=0.95),
        **_DEFAULTS,
    )
    v = heuristic_verdict_for_low(g)
    assert v.verdict == "replan"
    assert v.target_role == "planner"


# ── Misc ────────────────────────────────────────────────────────────────────


def test_metrics_preserved_in_result():
    sig = _signals(pytest_exit=1, todo_ratio=0.4)
    g = evaluate(sig, **_DEFAULTS)
    assert g.metrics["pytest_exit"] == 1
    assert g.metrics["todo_ratio"] == 0.4


@pytest.mark.parametrize("level,label", [
    ("HIGH", "충분"),
    ("MEDIUM", "보더라인"),
    ("LOW", "부족"),
])
def test_gate_level_label(level, label):
    assert gate_level_to_label(level) == label


# ── B-2: verifier 미호출 검출 (sufficiency 가 *낮은 신뢰* 로 처리하는지) ──
# sufficiency rule_gate 는 ``pytest_exit=None`` 을 "신호 없음" 으로 다뤄
# HIGH 분기를 *막지는* 않지만, 사용자 요청이 코드 작성 + 테스트인데
# verifier 가 한 번도 호출되지 않으면 (즉 pytest_exit 가 None 인데 todo
# 가 모두 completed 라고 마킹된 상태) sufficiency 가 *충분히 검증된 것이
# 아니다* 라는 증거를 critic 에게 넘겨야 한다.


def test_high_when_pytest_signal_missing_is_intentional():
    """pytest_exit=None + lint=None 은 *신호 없음* (verifier/reviewer 미호출).
    이게 HIGH 로 직행하면 critic 도 안 도는데, 그건 코드 도메인의 정상
    flow 라 의도된 동작. todo_ratio + prd_coverage 가 신뢰 신호."""
    g = evaluate(
        _signals(pytest_exit=None, lint_errors=None),
        **_DEFAULTS,
    )
    assert g.level == "HIGH"
    assert g.metrics["pytest_exit"] is None  # raw 신호 보존 — critic 이 알 수 있음


def test_medium_when_prd_coverage_borderline_with_no_pytest():
    """반대 케이스: pytest 신호 부재 + prd_coverage 보더라인 → MEDIUM 으로
    critic 호출. critic 이 raw metrics 로 verifier 누락을 인지 가능."""
    g = evaluate(
        _signals(pytest_exit=None, lint_errors=None, prd_coverage=0.7),
        **_DEFAULTS,
    )
    assert g.level == "MEDIUM"
    assert g.metrics["pytest_exit"] is None  # critic 에 그대로 전달됨


# ── 옵션 C: 산출물 누락 검증 (v12 회귀 fix) ──


def test_low_when_artifacts_missing_overrides_other_signals():
    """artifacts_missing 이 비어있지 않으면 다른 신호와 무관하게 LOW.
    SubAgent 가 COMPLETED 라고 주장해도 사용자 산출물 검증 실패면 차단."""
    g = evaluate(
        _signals(
            artifacts_missing=["prd", "spec"],
            pytest_exit=0, lint_errors=0,
            todo_ratio=1.0, prd_coverage=1.0,  # 다른 신호는 모두 양호
        ),
        **_DEFAULTS,
    )
    assert g.level == "LOW"
    assert any("artifacts_missing" in t for t in g.triggered_signals)
    assert "산출물 누락" in g.reason


def test_high_when_no_artifact_intent_at_all():
    """artifact_intent 가 비어 있으면 (사용자가 산출물 요청 안 함) 영향
    없음. 이전 동작 보존."""
    g = evaluate(
        _signals(artifacts_missing=[], artifact_intent=[]),
        **_DEFAULTS,
    )
    assert g.level == "HIGH"


def test_heuristic_artifacts_missing_routes_to_planner_replan():
    """LOW 휴리스틱: artifacts_missing 우선 → planner replan."""
    g = evaluate(
        _signals(artifacts_missing=["prd", "ledger"]),
        **_DEFAULTS,
    )
    v = heuristic_verdict_for_low(g)
    assert v.verdict == "replan"
    assert v.target_role == "planner"
    assert "PRD.md" in v.feedback_for_retry  # PRD 파일 명시
    assert "ledger" in v.feedback_for_retry  # ledger 안내
    assert "ask_user_question 답변만으론" in v.feedback_for_retry  # 핵심 안내
