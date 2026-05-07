"""P0 — write_todos / update_todo orchestrator ledger.

Verifies:

1. TodoStore replace/update/list/counts/reset semantics
2. write_todos tool replaces the ledger and returns a compact summary
3. update_todo flips one row and rejects unknown ids / bad statuses
4. Manager builds tools that share the same store
5. on_change callback fires after both write_todos and update_todo
6. Manager exposes get_todo_store + build_todo_tools + set_todo_change_callback
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from coding_agent.tools.todo_tool import (
    TodoItem,
    TodoStore,
    build_update_todo_tool,
    build_write_todos_tool,
    render_todo_summary,
)


# ── TodoStore unit tests ──────────────────────────────────────

def test_store_starts_empty() -> None:
    store = TodoStore()
    assert store.is_empty()
    assert store.list_items() == []
    assert store.counts() == {
        "pending": 0, "in_progress": 0, "completed": 0, "verify_failed": 0,
    }


def test_store_replace_preserves_order() -> None:
    store = TodoStore()
    items = [
        TodoItem(id="TASK-03", content="Third"),
        TodoItem(id="TASK-01", content="First"),
        TodoItem(id="TASK-02", content="Second"),
    ]
    out = store.replace(items)
    assert [t.id for t in out] == ["TASK-03", "TASK-01", "TASK-02"]
    assert store.counts()["pending"] == 3


def test_store_replace_overwrites_previous_list() -> None:
    store = TodoStore()
    store.replace([TodoItem(id="TASK-01", content="A")])
    store.replace([TodoItem(id="TASK-02", content="B")])
    items = store.list_items()
    assert len(items) == 1
    assert items[0].id == "TASK-02"


def test_store_update_changes_status() -> None:
    store = TodoStore()
    store.replace([TodoItem(id="TASK-01", content="A")])
    store.update("TASK-01", "in_progress")
    assert store.counts() == {
        "pending": 0, "in_progress": 1, "completed": 0, "verify_failed": 0,
    }
    store.update("TASK-01", "completed")
    assert store.counts()["completed"] == 1


def test_store_update_unknown_id_raises() -> None:
    store = TodoStore()
    store.replace([TodoItem(id="TASK-01", content="A")])
    with pytest.raises(KeyError):
        store.update("TASK-99", "in_progress")


def test_store_reset_clears_everything() -> None:
    store = TodoStore()
    store.replace([TodoItem(id="TASK-01", content="A")])
    store.reset()
    assert store.is_empty()


def test_store_replace_refuses_when_completed_entries_exist() -> None:
    # Guard: orchestrator must not wipe prior progress mid-run.
    store = TodoStore()
    store.replace([TodoItem(id="TASK-01", content="A")])
    store.update("TASK-01", "completed")
    with pytest.raises(ValueError, match="terminal"):
        store.replace([TodoItem(id="TASK-09", content="Fresh SPEC")])
    # Prior ledger must remain intact.
    items = store.list_items()
    assert len(items) == 1
    assert items[0].id == "TASK-01"
    assert items[0].status == "completed"


def test_store_replace_refuses_when_verify_failed_entries_exist() -> None:
    # v22.4 — verify_failed 도 terminal. replace 가 erase 못 하게 보호.
    store = TodoStore()
    store.replace([TodoItem(id="TASK-01", content="A")])
    store.update("TASK-01", "verify_failed")
    with pytest.raises(ValueError, match="terminal"):
        store.replace([TodoItem(id="TASK-09", content="Fresh SPEC")])
    items = store.list_items()
    assert len(items) == 1
    assert items[0].status == "verify_failed"


def test_write_todos_tool_returns_rejected_when_completed_exists() -> None:
    store = TodoStore()
    write = build_write_todos_tool(store=store)
    update = build_update_todo_tool(store=store)
    write.invoke({"todos": [{"id": "TASK-01", "content": "A"}]})
    update.invoke({"id": "TASK-01", "status": "completed"})
    out = write.invoke({"todos": [{"id": "TASK-09", "content": "Fresh SPEC"}]})
    assert "REJECTED" in out
    assert "update_todo" in out
    # Ledger must be untouched.
    items = store.list_items()
    assert len(items) == 1
    assert items[0].id == "TASK-01"


# ── render helper ─────────────────────────────────────────────

def test_render_summary_includes_counts_and_glyphs() -> None:
    items = [
        TodoItem(id="TASK-01", content="A", status="completed"),
        TodoItem(id="TASK-02", content="B", status="in_progress"),
        TodoItem(id="TASK-03", content="C", status="pending"),
    ]
    out = render_todo_summary(items)
    assert "pending=1" in out
    assert "in_progress=1" in out
    assert "completed=1" in out
    assert "TASK-01" in out and "TASK-02" in out and "TASK-03" in out
    assert "[x]" in out and "[~]" in out and "[ ]" in out


def test_render_summary_handles_empty() -> None:
    assert "empty" in render_todo_summary([]).lower()


# ── write_todos tool ──────────────────────────────────────────

def test_write_todos_tool_replaces_and_returns_summary() -> None:
    store = TodoStore()
    tool = build_write_todos_tool(store=store)
    result = tool.invoke(
        {
            "todos": [
                {"id": "TASK-01", "content": "Implement auth"},
                {"id": "TASK-02", "content": "Implement profile", "status": "pending"},
            ]
        }
    )
    assert "Todos: 2 total" in result
    assert "TASK-01" in result
    assert store.counts()["pending"] == 2


def test_write_todos_tool_replaces_previous_call() -> None:
    store = TodoStore()
    tool = build_write_todos_tool(store=store)
    tool.invoke({"todos": [{"id": "TASK-01", "content": "Old"}]})
    tool.invoke({"todos": [{"id": "TASK-02", "content": "New"}]})
    items = store.list_items()
    assert len(items) == 1
    assert items[0].id == "TASK-02"


def test_write_todos_tool_fires_on_change_callback() -> None:
    store = TodoStore()
    received: list = []
    tool = build_write_todos_tool(
        store=store, on_change=lambda items: received.append(list(items))
    )
    tool.invoke({"todos": [{"id": "TASK-01", "content": "A"}]})
    assert len(received) == 1
    assert received[0][0].id == "TASK-01"


def test_write_todos_tool_swallows_callback_errors() -> None:
    store = TodoStore()

    def boom(_items):
        raise RuntimeError("display crashed")

    tool = build_write_todos_tool(store=store, on_change=boom)
    # Must not raise — callbacks are best-effort.
    out = tool.invoke({"todos": [{"id": "TASK-01", "content": "A"}]})
    assert "TASK-01" in out


# ── update_todo tool ──────────────────────────────────────────

def test_update_todo_tool_marks_in_progress() -> None:
    store = TodoStore()
    write = build_write_todos_tool(store=store)
    update = build_update_todo_tool(store=store)
    write.invoke({"todos": [{"id": "TASK-01", "content": "A"}]})
    out = update.invoke({"id": "TASK-01", "status": "in_progress"})
    assert "in_progress=1" in out
    assert store.counts()["in_progress"] == 1


def test_update_todo_tool_rejects_unknown_id() -> None:
    store = TodoStore()
    update = build_update_todo_tool(store=store)
    out = update.invoke({"id": "TASK-99", "status": "completed"})
    assert "REJECTED" in out
    assert "TASK-99" in out


def test_update_todo_tool_rejects_verify_failed_directly() -> None:
    """v22.4 — verify_failed 는 harness-managed terminal status.

    orchestrator/SubAgent 가 ``update_todo`` 도구로 직접 마킹하면 거짓
    실패 신호를 만들 수 있어 reject. harness 의 ``_auto_advance_todo``
    가 store.update 를 직접 호출하는 경로만 허용.
    """
    store = TodoStore()
    write = build_write_todos_tool(store=store)
    update = build_update_todo_tool(store=store)
    write.invoke({"todos": [{"id": "TASK-01", "content": "A"}]})

    out = update.invoke({"id": "TASK-01", "status": "verify_failed"})
    assert "REJECTED" in out
    assert "verify_failed" in out
    assert "harness" in out
    # store 는 변경되지 않았어야 함 — 첫 status 그대로.
    assert store.list_items()[0].status == "pending"


def test_store_update_accepts_verify_failed_directly() -> None:
    """``TodoStore.update`` 자체는 verify_failed 허용 — harness 내부 경로용.

    `_auto_advance_todo` 가 이 경로로 호출. update_todo *tool* (LLM-facing)
    만 reject 하고, 직접 API 는 통과 — 두 경계의 정책이 다름.
    """
    store = TodoStore()
    store.replace([TodoItem(id="TASK-01", content="A")])
    store.update("TASK-01", "verify_failed")
    assert store.list_items()[0].status == "verify_failed"
    assert store.counts()["verify_failed"] == 1


def test_update_todo_tool_fires_on_change_callback() -> None:
    store = TodoStore()
    received: list = []
    write = build_write_todos_tool(store=store)
    update = build_update_todo_tool(
        store=store, on_change=lambda items: received.append(items)
    )
    write.invoke({"todos": [{"id": "TASK-01", "content": "A"}]})
    update.invoke({"id": "TASK-01", "status": "completed"})
    assert len(received) == 1
    assert received[0][0].status == "completed"


# ── AgentLoop integration ────────────────────────────────────
# Phase 6 refactor: TodoStore 는 이제 AgentLoop 가 직접 소유한다
# (manager 제거). write_todos / update_todo 가 같은 store 를 공유하는지
# + change callback 이 양쪽에서 발화되는지 검증.


def test_loop_exposes_todo_store_and_callback() -> None:
    from coding_agent.core.loop import AgentLoop

    loop = AgentLoop()
    store = loop.get_todo_store()
    assert isinstance(store, TodoStore)
    assert store.is_empty()

    received: list = []
    loop.set_todo_change_callback(lambda items: received.append(items))

    write_tool = build_write_todos_tool(
        store=store,
        on_change=lambda items: (
            loop._todo_change_callback(items)
            if loop._todo_change_callback
            else None
        ),
    )
    update_tool = build_update_todo_tool(
        store=store,
        on_change=lambda items: (
            loop._todo_change_callback(items)
            if loop._todo_change_callback
            else None
        ),
    )

    write_tool.invoke({"todos": [{"id": "TASK-01", "content": "A"}]})
    update_tool.invoke({"id": "TASK-01", "status": "completed"})
    assert len(received) == 2
    assert received[1][0].status == "completed"


# ── SYSTEM_PROMPT contract ───────────────────────────────────

def test_system_prompt_delegates_ledger_ops_to_ledger_subagent() -> None:
    """Orchestrator no longer owns write_todos/update_todo directly — it
    delegates to the ledger SubAgent. The prompt must still reference
    write_todos (as the ledger's action) and the ledger role, and must
    gate termination on pending == 0."""
    from coding_agent.core.loop import SYSTEM_PROMPT
    assert "write_todos" in SYSTEM_PROMPT
    assert "ledger" in SYSTEM_PROMPT
    # Must explicitly tell the model to keep going until pending == 0.
    assert "pending" in SYSTEM_PROMPT
