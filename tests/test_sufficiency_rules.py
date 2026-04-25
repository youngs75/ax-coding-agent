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
