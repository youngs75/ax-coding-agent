"""Decomposition gate — ledger 등록 후 사용자 granularity 확인 강제.

v2 E2E (2026-04-22): planner 가 "백엔드 API 개발" 같은 거대 묶음 8개로
분해하고 orchestrator 가 사용자 승인 없이 바로 coder 로 돌진 → coder 가
"완료" 기준을 자의적으로 판단. v1 은 반대로 46 tasks 로 과도 세분화.
어느 쪽이든 한 번은 사용자가 보고 조정할 수 있는 게이트가 필요.

여기서는 pure 함수 2개만 검증한다 (그래프는 세우지 않음).
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from coding_agent.core.loop import (
    _build_gate_decomposition_message,
    _detect_implicit_decomposition_confirm,
    _requires_decomposition_gate,
)


def _ai_with_task_call(agent_type: str, tool_call_id: str = "call_123", desc: str = "TASK-01: do thing") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "task",
                "args": {"description": desc, "agent_type": agent_type},
                "id": tool_call_id,
                "type": "tool_call",
            }
        ],
    )


# ── _requires_decomposition_gate: empty / confirmed / no-tool-call ──────────


def test_gate_skipped_when_confirmed():
    msg = _ai_with_task_call("coder")
    counts = {"pending": 10, "in_progress": 0, "completed": 0}
    assert _requires_decomposition_gate(msg, counts, confirmed=True) == (False, None)


def test_gate_skipped_when_ledger_empty():
    msg = _ai_with_task_call("coder")
    counts = {"pending": 0, "in_progress": 0, "completed": 0}
    assert _requires_decomposition_gate(msg, counts, confirmed=False) == (False, None)


def test_gate_skipped_when_no_tool_calls():
    msg = AIMessage(content="just text, no tools", tool_calls=[])
    counts = {"pending": 10, "in_progress": 0, "completed": 0}
    assert _requires_decomposition_gate(msg, counts, confirmed=False) == (False, None)


def test_gate_skipped_when_last_message_is_not_ai():
    msg = HumanMessage(content="hello")
    counts = {"pending": 10, "in_progress": 0, "completed": 0}
    assert _requires_decomposition_gate(msg, counts, confirmed=False) == (False, None)


def test_gate_skipped_when_last_message_is_none():
    counts = {"pending": 10, "in_progress": 0, "completed": 0}
    assert _requires_decomposition_gate(None, counts, confirmed=False) == (False, None)


# ── _requires_decomposition_gate: per-role behavior ─────────────────────────


def test_gate_passes_ledger_delegation():
    msg = _ai_with_task_call("ledger", tool_call_id="call_L", desc="register todos")
    counts = {"pending": 10, "in_progress": 0, "completed": 0}
    # ledger itself must be allowed — it's the one that fills the ledger.
    assert _requires_decomposition_gate(msg, counts, confirmed=False) == (False, None)


def test_gate_passes_planner_delegation():
    msg = _ai_with_task_call("planner", tool_call_id="call_P")
    counts = {"pending": 10, "in_progress": 0, "completed": 0}
    # planner re-delegation (e.g. for rework) should not be blocked.
    assert _requires_decomposition_gate(msg, counts, confirmed=False) == (False, None)


def test_gate_blocks_coder_delegation():
    msg = _ai_with_task_call("coder", tool_call_id="call_abc")
    counts = {"pending": 10, "in_progress": 0, "completed": 0}
    assert _requires_decomposition_gate(msg, counts, confirmed=False) == (True, "call_abc")


def test_gate_blocks_verifier_delegation():
    msg = _ai_with_task_call("verifier", tool_call_id="call_V")
    counts = {"pending": 5, "in_progress": 0, "completed": 5}  # total > 0
    assert _requires_decomposition_gate(msg, counts, confirmed=False) == (True, "call_V")


def test_gate_blocks_fixer_and_reviewer():
    for role in ("fixer", "reviewer"):
        msg = _ai_with_task_call(role, tool_call_id=f"call_{role}")
        counts = {"pending": 3, "in_progress": 0, "completed": 0}
        assert _requires_decomposition_gate(msg, counts, confirmed=False) == (True, f"call_{role}")


def test_gate_case_insensitive_agent_type():
    msg = _ai_with_task_call("CODER", tool_call_id="call_upper")
    counts = {"pending": 1, "in_progress": 0, "completed": 0}
    assert _requires_decomposition_gate(msg, counts, confirmed=False) == (True, "call_upper")


def test_gate_only_triggers_on_task_tool():
    # Even with a matching role-like arg, a non-"task" tool should not gate.
    msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_file",
                "args": {"path": "/tmp/x"},
                "id": "call_rf",
                "type": "tool_call",
            }
        ],
    )
    counts = {"pending": 10, "in_progress": 0, "completed": 0}
    assert _requires_decomposition_gate(msg, counts, confirmed=False) == (False, None)


def test_gate_picks_first_offending_call_when_multiple():
    # Parallel tool_calls: ledger (ok) + coder (gated). First coder wins.
    msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "task",
                "args": {"description": "register", "agent_type": "ledger"},
                "id": "call_L",
                "type": "tool_call",
            },
            {
                "name": "task",
                "args": {"description": "TASK-01: impl", "agent_type": "coder"},
                "id": "call_C",
                "type": "tool_call",
            },
        ],
    )
    counts = {"pending": 10, "in_progress": 0, "completed": 0}
    assert _requires_decomposition_gate(msg, counts, confirmed=False) == (True, "call_C")


# ── _build_gate_decomposition_message ───────────────────────────────────────


def test_gate_message_includes_preview_and_counts():
    counts = {"pending": 8, "in_progress": 0, "completed": 0}
    preview = [
        "TASK-01: 사용자 인증 및 역할 기반 접근 제어 구현",
        "TASK-02: 플랫폼 아키텍처 설계",
        "TASK-03: 데이터베이스 스키마 설계",
    ]
    msg = _build_gate_decomposition_message(counts, preview)

    assert "8개 task" in msg
    assert "TASK-01" in msg
    assert "TASK-02" in msg
    assert "TASK-03" in msg
    assert "ask_user_question" in msg
    assert "더 세분화" in msg
    assert "더 통합" in msg


def test_gate_message_shows_overflow_hint_when_many():
    counts = {"pending": 46, "in_progress": 0, "completed": 0}
    preview = [f"TASK-{i:02d}: item {i}" for i in range(1, 11)]  # 10 lines but only 5 shown

    msg = _build_gate_decomposition_message(counts, preview)

    assert "TASK-01" in msg
    assert "TASK-05" in msg
    assert "TASK-06" not in msg  # cap at 5
    # Preview is capped at 5; remainder = 46 - 5 = 41
    assert "외 41개" in msg


def test_gate_message_no_overflow_when_counts_match_preview():
    counts = {"pending": 3, "in_progress": 0, "completed": 0}
    preview = [
        "TASK-01: a",
        "TASK-02: b",
        "TASK-03: c",
    ]
    msg = _build_gate_decomposition_message(counts, preview)
    assert "외 " not in msg  # no overflow hint


# ── _detect_implicit_decomposition_confirm ──────────────────────────────────


def test_implicit_confirm_false_when_ledger_empty():
    messages = [
        ToolMessage(content="ok", tool_call_id="c1", name="ask_user_question"),
    ]
    counts = {"pending": 0, "in_progress": 0, "completed": 0}
    assert _detect_implicit_decomposition_confirm(messages, counts) is False


def test_implicit_confirm_false_when_no_ask_history():
    messages = [
        HumanMessage(content="hi"),
        AIMessage(content="ok", tool_calls=[]),
        ToolMessage(content="file content", tool_call_id="c1", name="read_file"),
    ]
    counts = {"pending": 10, "in_progress": 0, "completed": 0}
    assert _detect_implicit_decomposition_confirm(messages, counts) is False


def test_implicit_confirm_true_when_ask_answered_and_ledger_nonempty():
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "ask_user_question", "args": {}, "id": "c1", "type": "tool_call"}],
        ),
        ToolMessage(content="user answered", tool_call_id="c1", name="ask_user_question"),
    ]
    counts = {"pending": 26, "in_progress": 0, "completed": 0}
    assert _detect_implicit_decomposition_confirm(messages, counts) is True


def test_implicit_confirm_true_with_mixed_tool_history():
    # Multiple tool messages; ask_user_question present among them.
    messages = [
        ToolMessage(content="a", tool_call_id="c1", name="read_file"),
        ToolMessage(content="b", tool_call_id="c2", name="ask_user_question"),
        ToolMessage(content="c", tool_call_id="c3", name="execute"),
    ]
    counts = {"pending": 5, "in_progress": 1, "completed": 0}
    assert _detect_implicit_decomposition_confirm(messages, counts) is True


def test_implicit_confirm_false_on_empty_messages():
    counts = {"pending": 10, "in_progress": 0, "completed": 0}
    assert _detect_implicit_decomposition_confirm([], counts) is False


# ── Integration: implicit confirm blocks the gate via confirmed=True ────────


def test_gate_skipped_after_implicit_confirm_flips_flag():
    # Simulates check_progress flipping the flag based on implicit signal —
    # subsequent gate checks must now pass the coder delegation through.
    counts = {"pending": 10, "in_progress": 0, "completed": 0}
    msg = _ai_with_task_call("coder", tool_call_id="c_coder")
    assert _requires_decomposition_gate(msg, counts, confirmed=True) == (False, None)
