"""Todo tracking tool — orchestrator-side progress ledger.

Motivation
----------
v6 E2E showed the orchestrator forgetting half of the SPEC's atomic tasks
(TASK-07~12 frontend) and exiting with a natural-language reply instead of
continuing the implementation loop. The root cause is that LangGraph state
is just a message list — the orchestrator has no first-class "remaining
work" structure to consult between SubAgent delegations.

This tool gives the orchestrator a small persistent ledger keyed by task id.

Pattern borrowed from
  - Claude Code TodoWriteTool (full-list replacement, harness owns state)
  - DeepAgents write_todos     (per-thread, reflected back to LLM)
  - Codex update_plan          (incremental status updates)

Two complementary tools share one store:

* ``write_todos(todos)`` replaces the entire ledger. The orchestrator calls
  this once after reading SPEC to register every atomic task.
* ``update_todo(id, status)`` flips one entry. The orchestrator calls it
  before/after each delegation to mark progress.

Both tools return a compact summary the LLM can read in the next turn.
"""

from __future__ import annotations

import threading
from typing import Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

TodoStatus = Literal["pending", "in_progress", "completed"]

_VALID_STATUSES: tuple[str, ...] = ("pending", "in_progress", "completed")


class TodoItem(BaseModel):
    """One row in the orchestrator todo ledger."""

    id: str = Field(
        description=(
            "Stable identifier. Use the SPEC TASK-NN id when registering "
            "atomic tasks (e.g. 'TASK-01'). Required."
        ),
        min_length=1,
    )
    content: str = Field(
        description="Short human-readable description of the task.",
        min_length=1,
    )
    status: TodoStatus = Field(
        default="pending",
        description="One of: 'pending', 'in_progress', 'completed'.",
    )


class WriteTodosInput(BaseModel):
    todos: list[TodoItem] = Field(
        description=(
            "Full list of todos. This call REPLACES any previous list — "
            "include every task you intend to track, even ones already "
            "completed in earlier turns."
        ),
        min_length=1,
    )


class UpdateTodoInput(BaseModel):
    id: str = Field(
        description="The todo id to update (must already exist in the ledger).",
        min_length=1,
    )
    status: TodoStatus = Field(
        description="New status: 'pending', 'in_progress', or 'completed'.",
    )


class TodoStore:
    """Thread-safe ledger of orchestrator todos.

    Owned by ``SubAgentManager`` so the same store survives across multiple
    Orchestrator turns within a single user session, the same way
    ``_user_decisions`` does.
    """

    def __init__(self) -> None:
        self._items: dict[str, TodoItem] = {}
        self._order: list[str] = []  # preserves insertion order
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────

    def replace(self, todos: list[TodoItem]) -> list[TodoItem]:
        """Replace the entire ledger. Returns the new ordered list.

        Raises ``ValueError`` if the current ledger already contains any
        ``completed`` entry. Strategy: hard-reject rather than append-only,
        so the orchestrator surfaces the mistake instead of silently losing
        prior progress when it tries to re-register a fresh SPEC mid-run.
        """
        with self._lock:
            if any(item.status == "completed" for item in self._items.values()):
                completed_ids = [
                    i for i in self._order if self._items[i].status == "completed"
                ]
                raise ValueError(
                    "ledger already has completed entries "
                    f"({', '.join(completed_ids)}); use update_todo to "
                    "advance existing tasks instead of replacing the ledger."
                )
            self._items = {}
            self._order = []
            for t in todos:
                if t.id in self._items:
                    # duplicate id — last one wins, keep original position
                    self._items[t.id] = t
                    continue
                self._items[t.id] = t
                self._order.append(t.id)
            return self._ordered_unlocked()

    def update(self, todo_id: str, status: TodoStatus) -> TodoItem:
        """Flip one row's status. Raises ``KeyError`` if id is unknown."""
        with self._lock:
            existing = self._items.get(todo_id)
            if existing is None:
                raise KeyError(todo_id)
            updated = existing.model_copy(update={"status": status})
            self._items[todo_id] = updated
            return updated

    def list_items(self) -> list[TodoItem]:
        """Return todos in insertion order."""
        with self._lock:
            return self._ordered_unlocked()

    def counts(self) -> dict[str, int]:
        with self._lock:
            counts = {s: 0 for s in _VALID_STATUSES}
            for item in self._items.values():
                counts[item.status] = counts.get(item.status, 0) + 1
            return counts

    def reset(self) -> None:
        with self._lock:
            self._items = {}
            self._order = []

    def is_empty(self) -> bool:
        with self._lock:
            return not self._items

    # ── Internals ─────────────────────────────────────────────

    def _ordered_unlocked(self) -> list[TodoItem]:
        return [self._items[i] for i in self._order if i in self._items]


# ── Rendering helpers ──────────────────────────────────────────

_STATUS_GLYPHS: dict[str, str] = {
    "pending": "[ ]",
    "in_progress": "[~]",
    "completed": "[x]",
}


def render_todo_summary(items: list[TodoItem]) -> str:
    """Compact text representation returned to the LLM after each call."""
    if not items:
        return "Todo ledger is empty."
    counts = {s: 0 for s in _VALID_STATUSES}
    for it in items:
        counts[it.status] = counts.get(it.status, 0) + 1
    header = (
        f"Todos: {len(items)} total — "
        f"pending={counts['pending']}, "
        f"in_progress={counts['in_progress']}, "
        f"completed={counts['completed']}."
    )
    lines = [header]
    for it in items:
        glyph = _STATUS_GLYPHS.get(it.status, "[?]")
        lines.append(f"  {glyph} {it.id}: {it.content}")
    return "\n".join(lines)


# ── Tool factories ─────────────────────────────────────────────

OnChangeCallback = "callable"  # type: ignore[assignment]


def build_write_todos_tool(
    store: TodoStore,
    on_change=None,
) -> StructuredTool:
    """Return the ``write_todos`` StructuredTool bound to *store*.

    *on_change*, if provided, is invoked with the updated list after each
    successful write. Used by the CLI to refresh the rendered panel.
    """

    def _run(todos: list[TodoItem]) -> str:
        try:
            items = store.replace(todos)
        except ValueError as e:
            # Guard: ledger already has completed entries. Refuse to clobber
            # prior progress; orchestrator must use update_todo to advance.
            return f"REJECTED: {e}"
        if on_change is not None:
            try:
                on_change(items)
            except Exception:
                # Display callbacks must never break tool execution.
                pass
        return render_todo_summary(items)

    tool = StructuredTool.from_function(
        func=_run,
        name="write_todos",
        description=(
            "Register or replace the orchestrator's todo ledger. Pass the "
            "FULL list of tasks you plan to track — this call replaces any "
            "previous ledger. Use this once after reading SPEC to register "
            "every atomic task (use the SPEC TASK-NN id as the todo id). "
            "Returns a compact summary you can read in the next turn."
        ),
        args_schema=WriteTodosInput,
    )
    tool.metadata = {"todo_store": store}  # type: ignore[attr-defined]
    return tool


def build_update_todo_tool(
    store: TodoStore,
    on_change=None,
) -> StructuredTool:
    """Return the ``update_todo`` StructuredTool bound to *store*."""

    def _run(id: str, status: TodoStatus) -> str:
        if status not in _VALID_STATUSES:
            return (
                f"REJECTED: status must be one of "
                f"{', '.join(_VALID_STATUSES)} (got {status!r})."
            )
        try:
            store.update(id, status)
        except KeyError:
            return (
                f"REJECTED: unknown todo id {id!r}. "
                "Call write_todos first to register the ledger, "
                "or check the id matches an existing entry."
            )
        items = store.list_items()
        if on_change is not None:
            try:
                on_change(items)
            except Exception:
                pass
        return render_todo_summary(items)

    tool = StructuredTool.from_function(
        func=_run,
        name="update_todo",
        description=(
            "Mark one todo's status. Use this immediately before delegating "
            "a task ('in_progress') and immediately after the SubAgent "
            "completes ('completed'). Keep looping until no pending or "
            "in_progress todos remain. Returns the updated ledger."
        ),
        args_schema=UpdateTodoInput,
    )
    tool.metadata = {"todo_store": store}  # type: ignore[attr-defined]
    return tool
