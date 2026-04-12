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

from coding_agent.subagents.factory import SubAgentFactory
from coding_agent.subagents.manager import SubAgentManager
from coding_agent.subagents.registry import SubAgentRegistry
from coding_agent.tools.todo_tool import (
    TodoItem,
    TodoStore,
    build_update_todo_tool,
    build_write_todos_tool,
    render_todo_summary,
)


def _make_manager() -> SubAgentManager:
    registry = SubAgentRegistry()
    llm = MagicMock()
    factory = SubAgentFactory(registry, llm)
    return SubAgentManager(registry, factory)


# ── TodoStore unit tests ──────────────────────────────────────

def test_store_starts_empty() -> None:
    store = TodoStore()
    assert store.is_empty()
    assert store.list_items() == []
    assert store.counts() == {"pending": 0, "in_progress": 0, "completed": 0}


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
    assert store.counts() == {"pending": 0, "in_progress": 1, "completed": 0}
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


# ── Manager integration ──────────────────────────────────────

def test_manager_exposes_todo_store() -> None:
    manager = _make_manager()
    store = manager.get_todo_store()
    assert isinstance(store, TodoStore)
    assert store.is_empty()


def test_manager_build_todo_tools_returns_pair_sharing_store() -> None:
    manager = _make_manager()
    tools = manager.build_todo_tools()
    names = [t.name for t in tools]
    assert names == ["write_todos", "update_todo"]
    # Both tools must point at the SAME manager-owned store.
    assert tools[0].metadata["todo_store"] is manager.get_todo_store()
    assert tools[1].metadata["todo_store"] is manager.get_todo_store()


def test_manager_resolve_tools_wires_todo_pair() -> None:
    manager = _make_manager()
    write_tool = manager._resolve_tools(["write_todos"])[0]
    update_tool = manager._resolve_tools(["update_todo"])[0]
    assert write_tool.name == "write_todos"
    assert update_tool.name == "update_todo"
    assert write_tool.metadata["todo_store"] is manager.get_todo_store()
    assert update_tool.metadata["todo_store"] is manager.get_todo_store()


def test_manager_todo_change_callback_propagates() -> None:
    manager = _make_manager()
    received: list = []
    manager.set_todo_change_callback(lambda items: received.append(items))
    tools = manager.build_todo_tools()
    write_tool, update_tool = tools[0], tools[1]
    write_tool.invoke({"todos": [{"id": "TASK-01", "content": "A"}]})
    update_tool.invoke({"id": "TASK-01", "status": "completed"})
    assert len(received) == 2
    assert received[1][0].status == "completed"


def test_manager_todo_store_persists_across_resolve_calls() -> None:
    """Same TodoStore must be reused across multiple _resolve_tools calls
    so write_todos in turn N is visible to update_todo in turn N+1."""
    manager = _make_manager()
    write_tool = manager._resolve_tools(["write_todos"])[0]
    write_tool.invoke({"todos": [{"id": "TASK-01", "content": "A"}]})

    update_tool = manager._resolve_tools(["update_todo"])[0]
    out = update_tool.invoke({"id": "TASK-01", "status": "in_progress"})
    assert "REJECTED" not in out
    assert manager.get_todo_store().counts()["in_progress"] == 1


# ── SYSTEM_PROMPT contract ───────────────────────────────────

def test_system_prompt_documents_write_todos_step() -> None:
    """Ensure the orchestrator prompt actually instructs the model to use
    write_todos / update_todo. Without this, the slim prompt era shows
    the model never discovers the tools."""
    from coding_agent.core.loop import SYSTEM_PROMPT
    assert "write_todos" in SYSTEM_PROMPT
    assert "update_todo" in SYSTEM_PROMPT
    # Must explicitly tell the model to keep going until pending == 0.
    assert "pending" in SYSTEM_PROMPT
