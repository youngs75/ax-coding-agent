"""Termination-with-pending-todos 방어 로직 검증.

v12 E2E (2026-04-19): qwen3-max 가 62 todos 등록 후 `tool_calls=None` 으로
자연어만 뱉고 종료. v1 E2E (2026-04-21, 46 tasks): 매 배치 완료 후 동일한
silent-terminate 가 반복되어 누적 상한(3)이 소진됨 — 25/46 만 수행하고 종료.
이에 따라 카운터를 "진전 없는 연속 실패"로 재정의하고, progress 가 있는 한
계속 nudge 하도록 변경.

여기서는 그래프를 세우지 않고 추출된 pure 함수 2개만 검증한다.
"""

from __future__ import annotations

from coding_agent.core.loop import (
    _build_pending_nudge_message,
    _nudge_decision,
)
from coding_agent.tools.todo_tool import TodoItem


# ── _nudge_decision: clean_end ──────────────────────────────────────────────


def test_decision_clean_end_when_ledger_empty():
    counts = {"pending": 0, "in_progress": 0, "completed": 5}
    assert _nudge_decision(counts, last_unfinished=None, stuck_nudges=0, max_stuck=3) == "clean_end"


def test_decision_clean_end_ignores_prior_nudges():
    # Even if stuck counter is maxed, an empty ledger should terminate cleanly.
    counts = {"pending": 0, "in_progress": 0, "completed": 10}
    assert _nudge_decision(counts, last_unfinished=10, stuck_nudges=99, max_stuck=3) == "clean_end"


# ── _nudge_decision: nudge (first time) ─────────────────────────────────────


def test_decision_nudge_on_first_silent_terminate():
    # No prior nudge — baseline unknown, any pending should trigger a nudge.
    counts = {"pending": 62, "in_progress": 0, "completed": 0}
    assert _nudge_decision(counts, last_unfinished=None, stuck_nudges=0, max_stuck=3) == "nudge"


def test_decision_counts_in_progress_as_unfinished():
    counts = {"pending": 0, "in_progress": 2, "completed": 10}
    assert _nudge_decision(counts, last_unfinished=None, stuck_nudges=0, max_stuck=3) == "nudge"


# ── _nudge_decision: progress-based reset (core A) ──────────────────────────


def test_decision_nudge_when_progress_made_even_past_limit():
    # v1 E2E regression: 46→41 tasks completed between nudges. Even though
    # stuck_nudges is at 3, progress was made, so nudge should continue.
    # This is the fix — cumulative cap would terminate here.
    counts = {"pending": 41, "in_progress": 0, "completed": 5}
    assert _nudge_decision(counts, last_unfinished=46, stuck_nudges=3, max_stuck=3) == "nudge"
    # And well past the limit.
    assert _nudge_decision(counts, last_unfinished=46, stuck_nudges=99, max_stuck=3) == "nudge"


def test_decision_nudge_on_even_one_completed_task():
    # Tight progress signal: single task completion is enough to reset.
    counts = {"pending": 45, "in_progress": 0, "completed": 1}
    assert _nudge_decision(counts, last_unfinished=46, stuck_nudges=2, max_stuck=3) == "nudge"


# ── _nudge_decision: stuck_end (core C) ─────────────────────────────────────


def test_decision_stuck_end_when_no_progress_and_limit_reached():
    # Model keeps silent-terminating at same ledger state — give up.
    counts = {"pending": 10, "in_progress": 0, "completed": 0}
    assert _nudge_decision(counts, last_unfinished=10, stuck_nudges=3, max_stuck=3) == "stuck_end"


def test_decision_nudge_below_limit_even_without_progress():
    # Under the cap, still nudge once more — give the model another chance.
    counts = {"pending": 10, "in_progress": 0, "completed": 0}
    assert _nudge_decision(counts, last_unfinished=10, stuck_nudges=0, max_stuck=3) == "nudge"
    assert _nudge_decision(counts, last_unfinished=10, stuck_nudges=2, max_stuck=3) == "nudge"


def test_decision_stuck_end_treats_regression_as_stuck():
    # Pathological: unfinished went up (task re-added). Count as no progress.
    counts = {"pending": 12, "in_progress": 0, "completed": 0}
    assert _nudge_decision(counts, last_unfinished=10, stuck_nudges=3, max_stuck=3) == "stuck_end"


# ── _build_pending_nudge_message — unchanged ────────────────────────────────


def test_build_nudge_message_names_first_task_and_counts():
    item = TodoItem(id="TASK-01", content="프로젝트 정보 CRUD API 구현")
    counts = {"pending": 62, "in_progress": 0, "completed": 0}

    msg = _build_pending_nudge_message(item, counts)

    assert "TASK-01" in msg
    assert "프로젝트 정보 CRUD API 구현" in msg
    assert "pending=62" in msg
    assert "in_progress=0" in msg
    assert "task" in msg


def test_build_nudge_message_falls_back_when_no_first_item():
    msg = _build_pending_nudge_message(None, {"pending": 0, "in_progress": 0})

    assert "Termination blocked" in msg
    assert "task" in msg
