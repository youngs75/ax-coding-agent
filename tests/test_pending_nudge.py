"""Termination-with-pending-todos 방어 로직 검증 (v12 E2E 회귀).

qwen3-max 가 62개 todo 를 등록하고 `tool_calls=None` 으로 자연어만 뱉으며
종료한 사례(2026-04-19 v12). SYSTEM_PROMPT 에 "pending 모두 0 이어야 종료"
규칙이 있지만 모델 순종도에 의존하면 깨지므로, harness 가
``_should_nudge_pending`` + nudge 노드로 재시도를 강제한다.

여기서는 그래프를 세우지 않고 추출된 pure 함수 2개만 검증한다.
"""

from __future__ import annotations

from coding_agent.core.loop import (
    _build_pending_nudge_message,
    _should_nudge_pending,
)
from coding_agent.tools.todo_tool import TodoItem


def test_should_nudge_when_pending_exists_and_under_limit():
    counts = {"pending": 62, "in_progress": 0, "completed": 0}
    assert _should_nudge_pending(counts, nudges_so_far=0, max_nudges=3) is True
    assert _should_nudge_pending(counts, nudges_so_far=2, max_nudges=3) is True


def test_should_not_nudge_when_all_done():
    counts = {"pending": 0, "in_progress": 0, "completed": 5}
    assert _should_nudge_pending(counts, nudges_so_far=0, max_nudges=3) is False


def test_should_not_nudge_when_nudge_limit_reached():
    # Budget exhausted — accept the orchestrator's termination even if the
    # ledger still has pending items. Prevents infinite "nudge → no tool →
    # nudge" loops when a model genuinely refuses to follow the directive.
    counts = {"pending": 10, "in_progress": 0, "completed": 0}
    assert _should_nudge_pending(counts, nudges_so_far=3, max_nudges=3) is False
    assert _should_nudge_pending(counts, nudges_so_far=99, max_nudges=3) is False


def test_should_nudge_counts_in_progress_as_unfinished():
    # in_progress items also block termination — fixer hasn't run yet etc.
    counts = {"pending": 0, "in_progress": 2, "completed": 10}
    assert _should_nudge_pending(counts, nudges_so_far=0, max_nudges=3) is True


def test_build_nudge_message_names_first_task_and_counts():
    item = TodoItem(id="TASK-01", content="프로젝트 정보 CRUD API 구현")
    counts = {"pending": 62, "in_progress": 0, "completed": 0}

    msg = _build_pending_nudge_message(item, counts)

    assert "TASK-01" in msg
    assert "프로젝트 정보 CRUD API 구현" in msg
    assert "pending=62" in msg
    assert "in_progress=0" in msg
    assert "task" in msg  # tells the model to call the task tool


def test_build_nudge_message_falls_back_when_no_first_item():
    # Race: route decided there's pending work, but the ledger was cleared
    # before the nudge node ran. Emit a generic reminder instead of crashing.
    msg = _build_pending_nudge_message(None, {"pending": 0, "in_progress": 0})

    assert "Termination blocked" in msg
    assert "task" in msg
