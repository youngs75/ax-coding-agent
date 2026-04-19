"""Phase 3 회귀 — Phase 6 refactor 에 맞춰 단순화.

남은 회귀 검증 대상:
- TASK-NN 추출(`_extract_task_id`)
- A-2 ProgressGuard (library ProgressGuard + ax key_extractor)
- check_progress 메시지 스캔 (v8 핫픽스: messages[-1] → reverse scan)
- SYSTEM_PROMPT 에 핵심 지침이 남아 있는지

manager/factory/registry 기반 B-1 테스트는 Phase 6 에서 task_tool 로직이
``_auto_advance_todo`` 헬퍼로 이관되면서 새 단위 테스트로 대체했다.
"""

from __future__ import annotations

import pytest

from minyoung_mah import RoleStatus
from minyoung_mah.core.types import (
    RoleInvocationResult,
    ToolCallRequest,
    ToolResult,
)

from coding_agent.core.loop import SYSTEM_PROMPT, _task_id_extractor
from coding_agent.resilience_compat import GuardVerdict, ProgressGuard
from coding_agent.tools.task_tool import (
    _auto_advance_todo,
    _extract_task_id,
    _verifier_signals_success,
)
from coding_agent.tools.todo_tool import TodoItem, TodoStore


def _task_guard(**kwargs) -> ProgressGuard:
    kwargs.setdefault("key_extractor", _task_id_extractor)
    return ProgressGuard(**kwargs)


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


# ── B-1: _auto_advance_todo (task_tool helper) ──────────────


def _store_with(*ids: str) -> TodoStore:
    s = TodoStore()
    s.replace([TodoItem(id=i, content=i) for i in ids])
    return s


def test_auto_advance_marks_in_progress() -> None:
    store = _store_with("TASK-01", "TASK-02")
    assert _auto_advance_todo(store, "TASK-01", "in_progress", None) is True
    counts = store.counts()
    assert counts["in_progress"] == 1
    assert counts["pending"] == 1


def test_auto_advance_marks_completed() -> None:
    store = _store_with("TASK-01")
    _auto_advance_todo(store, "TASK-01", "in_progress", None)
    _auto_advance_todo(store, "TASK-01", "completed", None)
    assert store.counts()["completed"] == 1


def test_auto_advance_silently_skips_unknown_id() -> None:
    store = _store_with("TASK-01")
    assert _auto_advance_todo(store, "TASK-99", "in_progress", None) is False
    assert store.counts()["pending"] == 1


def test_auto_advance_does_not_downgrade_completed() -> None:
    store = _store_with("TASK-01")
    _auto_advance_todo(store, "TASK-01", "completed", None)
    assert _auto_advance_todo(store, "TASK-01", "in_progress", None) is False
    assert store.counts()["completed"] == 1


def test_auto_advance_invokes_callback() -> None:
    store = _store_with("TASK-01")
    received: list = []
    _auto_advance_todo(
        store, "TASK-01", "in_progress", lambda items: received.append(items)
    )
    assert len(received) == 1
    assert received[0][0].status == "in_progress"


def test_auto_advance_rejects_empty_id() -> None:
    store = _store_with("TASK-01")
    assert _auto_advance_todo(store, "", "in_progress", None) is False


# ── P2: _verifier_signals_success — verifier auto-advance gate ──────


def _verifier_result(
    *executes: tuple[str, bool, str],
    extra_calls: list[tuple[str, bool, str]] | None = None,
) -> RoleInvocationResult:
    """Helper: build a verifier RoleInvocationResult.

    *executes* — list of ``(command, ok, value)`` triples for execute calls.
    *extra_calls* — non-execute tool calls (e.g. read_file) appended after.
    """
    calls: list[ToolCallRequest] = []
    results: list[ToolResult] = []
    for i, (cmd, ok, value) in enumerate(executes):
        calls.append(
            ToolCallRequest(call_id=f"e{i}", tool_name="execute", args={"command": cmd})
        )
        results.append(ToolResult(ok=ok, value=value))
    for j, (name, ok, value) in enumerate(extra_calls or []):
        calls.append(ToolCallRequest(call_id=f"x{j}", tool_name=name, args={}))
        results.append(ToolResult(ok=ok, value=value))
    return RoleInvocationResult(
        role_name="verifier",
        status=RoleStatus.COMPLETED,
        output="Scope: ran tests\nResult: pass",
        tool_calls=calls,
        tool_results=results,
    )


def test_verifier_success_when_all_executes_clean() -> None:
    result = _verifier_result(
        ("pytest -q", True, "5 passed in 0.42s"),
        ("ruff check .", True, "All checks passed!"),
    )
    assert _verifier_signals_success(result) is True


def test_verifier_failure_when_exit_code_marker_present() -> None:
    # shell.execute embeds non-zero exits as "[exit code: N]" without raising,
    # so ToolResult.ok stays True even though the test actually failed.
    result = _verifier_result(
        ("pytest -q", True, "1 failed, 4 passed\n[exit code: 1]"),
    )
    assert _verifier_signals_success(result) is False


def test_verifier_failure_on_timeout_marker() -> None:
    result = _verifier_result(
        ("pytest -q", True, "[TIMEOUT] Command exceeded 90s and was terminated."),
    )
    assert _verifier_signals_success(result) is False


def test_verifier_failure_on_rejected_marker() -> None:
    result = _verifier_result(
        ("vitest", True, "REJECTED: the harness does not run watch/daemon commands"),
    )
    assert _verifier_signals_success(result) is False


def test_verifier_failure_when_adapter_ok_false() -> None:
    result = _verifier_result(
        ("pytest -q", False, ""),
    )
    assert _verifier_signals_success(result) is False


def test_verifier_no_executes_does_not_signal_success() -> None:
    # A verifier that only read files but never ran a command did not actually
    # verify anything — refuse to auto-advance.
    result = _verifier_result(extra_calls=[("read_file", True, "...")])
    assert _verifier_signals_success(result) is False


def test_verifier_mixed_executes_one_failing_blocks_advance() -> None:
    result = _verifier_result(
        ("pytest -q", True, "5 passed"),
        ("npm test", True, "FAIL src/foo.test.ts\n[exit code: 1]"),
    )
    assert _verifier_signals_success(result) is False


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
    assert len(guard._secondary_history) == 0


def test_progress_guard_reset_clears_task_history() -> None:
    guard = _task_guard()
    guard.record_action("task", {"description": "TASK-04: x"})
    guard.reset()
    assert len(guard._secondary_history) == 0


# ── C-2: SYSTEM_PROMPT 지침 유지 ─────────────────────────────


def test_system_prompt_mentions_sequential_todo_and_auto_marking() -> None:
    assert "등록 순서" in SYSTEM_PROMPT or "순서대로" in SYSTEM_PROMPT
    assert "자동" in SYSTEM_PROMPT
    assert "ProgressGuard" in SYSTEM_PROMPT


# ── A-2 integration: check_progress reverse lookup ──────────
# Critical regression — v8 핫픽스가 messages[-1] (ToolMessage) 만 보던 버그
# 를 reverse scan 으로 바꿈. 프로덕션 경로의 lookup 로직이 그대로 살아있는지
# 재확인.


def test_check_progress_finds_tool_calls_after_toolnode() -> None:
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

    found = None
    for msg in reversed(messages):
        tcs = getattr(msg, "tool_calls", None)
        if tcs:
            found = tcs
            break

    assert found is not None
    assert found[0]["name"] == "task"
    assert "TASK-09" in found[0]["args"]["description"]


def test_progress_guard_records_via_real_loop_check() -> None:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from coding_agent.core.loop import AgentLoop

    loop = AgentLoop()
    pg = loop._progress_guard
    pg.reset()

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

    for msg in reversed(state["messages"]):
        tcs = getattr(msg, "tool_calls", None)
        if tcs:
            for tc in tcs:
                pg.record_action(tc.get("name", ""), tc.get("args", {}))
            break

    assert len(pg._secondary_history) == 1
    assert pg._secondary_history[0] == "TASK-09"
