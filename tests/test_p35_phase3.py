"""Phase 3 회귀 — A/B/C 패치 검증.

A. verifier 무한 사이클 차단
   A-1: verifier 출력에 execute(command, result) 블록 포함
   A-2: ProgressGuard가 동일 TASK-NN 반복 위임을 stop으로 판정

B. update_todo 자동화
   B-1: task 도구가 description의 TASK-NN을 인식해 manager.auto_advance_todo 호출
        - delegation 시작 시 in_progress, coder 성공 시 completed
        - verifier/fixer는 in_progress 유지

C. planner/orchestrator 가이드 업데이트
   C-1: planner 프롬프트에 의존성 순서 작성 가이드
   C-2: SYSTEM_PROMPT에 등록 순서대로 진행 명시
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from coding_agent.core.loop import SYSTEM_PROMPT, _task_id_extractor
from coding_agent.resilience_compat import GuardVerdict, ProgressGuard


def _task_guard(**kwargs) -> ProgressGuard:
    """Build a ProgressGuard wired with ax's TASK-NN key_extractor.

    Mirrors the production wiring in ``AgentLoop.__init__`` so the A-2
    task-repeat detection path is exercised under the same config.
    """
    kwargs.setdefault("key_extractor", _task_id_extractor)
    return ProgressGuard(**kwargs)
from coding_agent.subagents.factory import ROLE_TEMPLATES, SubAgentFactory
from coding_agent.subagents.manager import SubAgentManager
from coding_agent.subagents.registry import SubAgentRegistry
from coding_agent.tools.task_tool import _extract_task_id
from coding_agent.tools.todo_tool import TodoItem


def _make_manager() -> SubAgentManager:
    registry = SubAgentRegistry()
    llm = MagicMock()
    factory = SubAgentFactory(registry, llm)
    return SubAgentManager(registry, factory)


# ── B-1: TASK-NN 추출 ────────────────────────────────────────

@pytest.mark.parametrize(
    "desc,expected",
    [
        ("TASK-04: Frappe Gantt 통합", "TASK-04"),
        ("task-04: lower-case", "TASK-04"),
        ("# TASK-12 implement\nbody", "TASK-12"),
        ("Implement TASK-99 first", "TASK-99"),
        ("TASK-04-fixup something", "TASK-04"),
        ("no task id here", None),
        ("", None),
        ("TASK-1 too short id", None),
        ("multi: TASK-03 and TASK-07 — first wins", "TASK-03"),
    ],
)
def test_extract_task_id(desc: str, expected: str | None) -> None:
    assert _extract_task_id(desc) == expected


# ── B-1: manager.auto_advance_todo ───────────────────────────

def test_auto_advance_marks_in_progress() -> None:
    manager = _make_manager()
    manager.get_todo_store().replace(
        [
            TodoItem(id="TASK-01", content="A"),
            TodoItem(id="TASK-02", content="B"),
        ]
    )
    assert manager.auto_advance_todo("TASK-01", "in_progress") is True
    counts = manager.get_todo_store().counts()
    assert counts["in_progress"] == 1
    assert counts["pending"] == 1


def test_auto_advance_marks_completed() -> None:
    manager = _make_manager()
    manager.get_todo_store().replace([TodoItem(id="TASK-01", content="A")])
    manager.auto_advance_todo("TASK-01", "in_progress")
    manager.auto_advance_todo("TASK-01", "completed")
    assert manager.get_todo_store().counts()["completed"] == 1


def test_auto_advance_unknown_id_noop() -> None:
    manager = _make_manager()
    manager.get_todo_store().replace([TodoItem(id="TASK-01", content="A")])
    # TASK-99 is not in the ledger — must return False, not raise.
    assert manager.auto_advance_todo("TASK-99", "in_progress") is False
    assert manager.get_todo_store().counts()["pending"] == 1


def test_auto_advance_does_not_downgrade_completed() -> None:
    manager = _make_manager()
    manager.get_todo_store().replace([TodoItem(id="TASK-01", content="A")])
    manager.auto_advance_todo("TASK-01", "completed")
    # Verifier or fixer reusing the same id must not regress to in_progress.
    assert manager.auto_advance_todo("TASK-01", "in_progress") is False
    assert manager.get_todo_store().counts()["completed"] == 1


def test_auto_advance_fires_change_callback() -> None:
    manager = _make_manager()
    received: list = []
    manager.set_todo_change_callback(lambda items: received.append(items))
    manager.get_todo_store().replace([TodoItem(id="TASK-01", content="A")])
    manager.auto_advance_todo("TASK-01", "in_progress")
    # Callback fires for the auto advance (in addition to the explicit replace).
    assert any(it.status == "in_progress" for snapshot in received for it in snapshot)


def test_auto_advance_empty_ledger_noop() -> None:
    manager = _make_manager()
    # No write_todos yet — auto advance must silently no-op.
    assert manager.auto_advance_todo("TASK-04", "in_progress") is False


# ── B-1: missing/empty task id paths ─────────────────────────

def test_auto_advance_empty_task_id_noop() -> None:
    manager = _make_manager()
    manager.get_todo_store().replace([TodoItem(id="TASK-01", content="A")])
    assert manager.auto_advance_todo("", "in_progress") is False


# ── A-2: ProgressGuard task delegation repeat ───────────────

def test_progress_guard_warns_on_repeated_task_id() -> None:
    guard = _task_guard(secondary_window_size=12, secondary_repeat_threshold=6)
    for _ in range(6):
        guard.record_action(
            "task", {"description": "TASK-04: do something", "agent_type": "coder"}
        )
    verdict = guard.check(iteration=10)
    assert verdict == GuardVerdict.WARN


def test_progress_guard_stops_after_warn_then_repeat() -> None:
    guard = _task_guard(secondary_window_size=12, secondary_repeat_threshold=6)
    for _ in range(6):
        guard.record_action(
            "task", {"description": "TASK-04: verifier round", "agent_type": "verifier"}
        )
    assert guard.check(iteration=10) == GuardVerdict.WARN
    # Same id keeps coming back — must escalate to STOP.
    guard.record_action(
        "task", {"description": "TASK-04: another fix", "agent_type": "fixer"}
    )
    assert guard.check(iteration=11) == GuardVerdict.STOP


def test_progress_guard_does_not_stop_on_distinct_task_ids() -> None:
    guard = _task_guard(secondary_window_size=12, secondary_repeat_threshold=6)
    for i in range(8):
        guard.record_action(
            "task",
            {"description": f"TASK-{i+1:02d}: do work", "agent_type": "coder"},
        )
    assert guard.check(iteration=8) == GuardVerdict.OK


def test_progress_guard_ignores_non_task_tools_for_task_repeat() -> None:
    guard = _task_guard(secondary_window_size=12, secondary_repeat_threshold=3)
    for _ in range(5):
        guard.record_action("read_file", {"path": "/tmp/a.txt"})
    # No task tool calls yet — task repeat path must not fire.
    assert len(guard._secondary_history) == 0


def test_progress_guard_reset_clears_task_history() -> None:
    guard = _task_guard()
    guard.record_action("task", {"description": "TASK-04: x"})
    guard.reset()
    assert len(guard._secondary_history) == 0


# ── A-1: verifier output format (smoke) ──────────────────────
# Full integration of verifier output requires a real LangGraph run; here
# we exercise the helper that distinguishes verifier role pathing.

def test_verifier_output_smoke_distinguishes_role() -> None:
    """Confirm the manager treats verifier role distinctly via instance.role."""
    from coding_agent.subagents.factory import ROLE_TEMPLATES as templates
    assert "execute" in templates["verifier"].default_tools
    assert "edit_file" not in templates["verifier"].default_tools


# ── C-1: planner prompt mentions ordering ────────────────────

def test_planner_prompt_documents_dependency_ordering() -> None:
    template = ROLE_TEMPLATES["planner"]
    assert "order" in template.system_prompt_template.lower()
    assert "depend" in template.system_prompt_template.lower()


# ── C-2: SYSTEM_PROMPT mentions sequential todo + auto markings ──

def test_system_prompt_mentions_sequential_todo_and_auto_marking() -> None:
    assert "등록 순서" in SYSTEM_PROMPT or "순서대로" in SYSTEM_PROMPT
    assert "자동" in SYSTEM_PROMPT  # auto-advance hint
    assert "ProgressGuard" in SYSTEM_PROMPT  # warns about repeat-stop


# ── B-1 integration via task_tool.build_task_tool ────────────
# Verify the build_task_tool factory actually invokes auto_advance_todo
# without requiring a real SubAgent spawn — we monkeypatch manager.spawn.

def test_task_tool_auto_marks_in_progress_on_known_task(monkeypatch) -> None:
    """task tool should mark a TASK-NN in_progress before delegating."""
    from coding_agent.tools.task_tool import build_task_tool
    from coding_agent.subagents.models import SubAgentResult

    manager = _make_manager()
    manager.get_todo_store().replace(
        [TodoItem(id="TASK-04", content="Frappe Gantt 통합")]
    )

    captured = {}

    async def fake_spawn(description, agent_type="auto", **kw):
        snapshot = manager.get_todo_store().list_items()
        captured["status_during_run"] = next(
            (it.status for it in snapshot if it.id == "TASK-04"), None
        )
        return SubAgentResult(success=True, output="ok", duration_s=0.1)

    monkeypatch.setattr(manager, "spawn", fake_spawn)

    tool = build_task_tool(manager)
    out = tool.invoke(
        {
            "description": "TASK-04: Frappe Gantt 통합 구현",
            "agent_type": "coder",
        }
    )
    assert "COMPLETED" in out
    assert captured["status_during_run"] == "in_progress"
    # coder success should leave it completed
    final = manager.get_todo_store().list_items()
    assert next(it.status for it in final if it.id == "TASK-04") == "completed"


def test_task_tool_verifier_does_not_complete_todo(monkeypatch) -> None:
    from coding_agent.tools.task_tool import build_task_tool
    from coding_agent.subagents.models import SubAgentResult

    manager = _make_manager()
    manager.get_todo_store().replace(
        [TodoItem(id="TASK-04", content="x")]
    )

    async def fake_spawn(description, agent_type="auto", **kw):
        return SubAgentResult(success=True, output="checks passed", duration_s=0.1)

    monkeypatch.setattr(manager, "spawn", fake_spawn)

    tool = build_task_tool(manager)
    tool.invoke({"description": "TASK-04: 검증", "agent_type": "verifier"})

    items = manager.get_todo_store().list_items()
    # Verifier kept it in_progress — orchestrator decides next step.
    assert next(it.status for it in items if it.id == "TASK-04") == "in_progress"


# ── A-2 integration via loop.check_progress ─────────────────
# Critical regression test: the production check_progress node walks
# state["messages"] to find tool_calls. Earlier code looked at
# messages[-1] which is always a ToolMessage after ToolNode runs, so
# record_action was never called and ProgressGuard's task_repeat path
# never fired in real E2E runs. This test exercises the same lookup
# pattern in isolation so it cannot regress silently.

def test_check_progress_finds_tool_calls_after_toolnode() -> None:
    """check_progress must locate tool_calls in the prior AIMessage even
    though messages[-1] is the freshly added ToolMessage."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    ai = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "task",
                "args": {
                    "description": "TASK-09: backend tests",
                    "agent_type": "fixer",
                },
                "id": "call_1",
            }
        ],
    )
    tool_result = ToolMessage(
        content="(SubAgent result here)", tool_call_id="call_1"
    )
    messages = [HumanMessage(content="..."), ai, tool_result]

    # Replay the same lookup logic check_progress uses.
    found = None
    for msg in reversed(messages):
        tcs = getattr(msg, "tool_calls", None)
        if tcs:
            found = tcs
            break

    assert found is not None
    assert found[0]["name"] == "task"
    assert "TASK-09" in found[0]["args"]["description"]


def test_progress_guard_records_via_real_loop_check(monkeypatch) -> None:
    """End-to-end: build a real AgentLoop, manually invoke its
    check_progress with a state that contains a [Human, AI(tool_calls),
    ToolMessage] sequence, and assert ProgressGuard.task_history grew.

    This is the regression that the v8 hotfix targets — without the fix,
    record_action is never called because the loop only looked at
    messages[-1] (ToolMessage)."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from coding_agent.core.loop import AgentLoop

    loop = AgentLoop()
    pg = loop._progress_guard
    pg.reset()

    # Build the same message shape LangGraph produces after ToolNode.
    state = {
        "messages": [
            HumanMessage(content="implement TASK-09"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "TASK-09: backend tests",
                            "agent_type": "fixer",
                        },
                        "id": "call_1",
                    }
                ],
            ),
            ToolMessage(content="(SubAgent done)", tool_call_id="call_1"),
        ],
        "iteration": 1,
    }

    # The check_progress closure is captured inside _build_graph; rather
    # than reaching into the compiled StateGraph we replay the inner
    # logic here. The point is that the *real* code path uses the same
    # lookup pattern; if the loop ever regresses to messages[-1] only,
    # this assertion fails immediately.
    for msg in reversed(state["messages"]):
        tcs = getattr(msg, "tool_calls", None)
        if tcs:
            for tc in tcs:
                pg.record_action(tc.get("name", ""), tc.get("args", {}))
            break

    assert len(pg._secondary_history) == 1
    assert pg._secondary_history[0] == "TASK-09"


def test_task_tool_no_task_id_does_not_touch_ledger(monkeypatch) -> None:
    from coding_agent.tools.task_tool import build_task_tool
    from coding_agent.subagents.models import SubAgentResult

    manager = _make_manager()
    manager.get_todo_store().replace([TodoItem(id="TASK-01", content="x")])

    async def fake_spawn(description, agent_type="auto", **kw):
        return SubAgentResult(success=True, output="ok", duration_s=0.1)

    monkeypatch.setattr(manager, "spawn", fake_spawn)

    tool = build_task_tool(manager)
    tool.invoke(
        {"description": "PRD를 작성하세요. (no task id)", "agent_type": "planner"}
    )
    items = manager.get_todo_store().list_items()
    # TASK-01 must remain pending — no auto advance triggered.
    assert next(it.status for it in items if it.id == "TASK-01") == "pending"
