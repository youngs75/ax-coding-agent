"""Decomposition gate — ledger 등록 후 사용자 granularity 확인 강제.

v2 E2E (2026-04-22): planner 가 "백엔드 API 개발" 같은 거대 묶음 8개로
분해하고 orchestrator 가 사용자 승인 없이 바로 coder 로 돌진 → coder 가
"완료" 기준을 자의적으로 판단. v1 은 반대로 46 tasks 로 과도 세분화.
어느 쪽이든 한 번은 사용자가 보고 조정할 수 있는 게이트가 필요.

v7 회귀 (2026-04-26): SYSTEM_PROMPT 의 분해 확인 섹션이 정적이라
``decomposition_confirmed`` 가 True 가 된 후에도 같은 안내가 보임 →
orchestrator 가 답을 받고도 planner 에게 같은 ask 를 위임하는 무한 루프.
SYSTEM_PROMPT 동적 섹션 + user_decisions block 추가로 해결.

여기서는 pure 함수만 검증한다 (그래프는 세우지 않음).
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from coding_agent.core.loop import (
    SYSTEM_PROMPT,
    _build_decomposition_section,
    _build_gate_decomposition_message,
    _build_user_decisions_block,
    _detect_implicit_decomposition_confirm,
    _requires_decomposition_gate,
)
from coding_agent.subagents.user_decisions import UserDecisionsLog


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


def _ai_with_task_to_ledger() -> AIMessage:
    """orchestrator 가 ledger SubAgent 에게 등록을 위임하는 AIMessage."""
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "task",
            "args": {"description": "register tasks", "agent_type": "ledger"},
            "id": "ledger_call",
            "type": "tool_call",
        }],
    )


def test_implicit_confirm_true_when_ask_after_ledger_delegation():
    """ledger 위임 *이후* 의 ask 는 confirm 으로 인정 — v3 케이스 보호."""
    messages = [
        HumanMessage(content="user request"),
        _ai_with_task_to_ledger(),
        ToolMessage(content="14 todos registered", tool_call_id="ledger_call", name="task"),
        AIMessage(
            content="",
            tool_calls=[{"name": "ask_user_question", "args": {}, "id": "c1", "type": "tool_call"}],
        ),
        ToolMessage(content="user answered", tool_call_id="c1", name="ask_user_question"),
    ]
    counts = {"pending": 14, "in_progress": 0, "completed": 0}
    assert _detect_implicit_decomposition_confirm(messages, counts) is True


def test_implicit_confirm_false_when_ask_only_before_ledger():
    """v8 회귀: planner 의 기술 스택 ask (ledger 등록 *전*) 만 있으면 confirm
    되면 안 된다. 사용자가 분해 ask 를 못 본 채 게이트가 풀리는 것 방지."""
    messages = [
        HumanMessage(content="user request"),
        # planner SubAgent 안에서 일어난 ask (top-level 에 ToolMessage 로 표면화)
        ToolMessage(content="user answered tech stack", tool_call_id="c1", name="ask_user_question"),
        # 그 다음 ledger 위임
        _ai_with_task_to_ledger(),
        ToolMessage(content="14 todos registered", tool_call_id="ledger_call", name="task"),
    ]
    counts = {"pending": 14, "in_progress": 0, "completed": 0}
    assert _detect_implicit_decomposition_confirm(messages, counts) is False


def test_implicit_confirm_false_when_no_ledger_delegation_yet():
    """ledger 가 채워졌지만 messages 에 task("ledger") 자취가 없으면 — 시점
    판별 불가 → 보수적으로 False (gate 정상 작동)."""
    messages = [
        ToolMessage(content="user answered", tool_call_id="c1", name="ask_user_question"),
    ]
    counts = {"pending": 14, "in_progress": 0, "completed": 0}
    assert _detect_implicit_decomposition_confirm(messages, counts) is False


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


# ── SYSTEM_PROMPT dynamic decomposition section (v7 회귀 fix) ──────────────


def test_decomposition_section_pending_shows_ask_instructions():
    text = _build_decomposition_section(False)
    assert "반드시 사용자에게 분해 granularity" in text
    assert "ask_user_question" in text
    # confirmed 텍스트가 섞이면 안 됨
    assert "분해 확인 — 완료" not in text


def test_decomposition_section_confirmed_forbids_repeat_ask():
    text = _build_decomposition_section(True)
    assert "분해 확인 — 완료" in text
    # 핵심 회귀 방어: 같은 질문 반복 / planner ask 위임 금지가 명시
    assert "같은 분해 확인 질문을 다시 호출" in text
    assert "planner" in text  # planner 위임 금지 안내
    # pending 안내가 사라져야 함
    assert "반드시 사용자에게 분해 granularity 를" not in text


def test_user_decisions_block_empty_when_no_records():
    assert _build_user_decisions_block("") == ""


def test_user_decisions_block_renders_log_header():
    ud = UserDecisionsLog()
    ud.record("User answered — 작업 분해: 이대로 진행")
    block = _build_user_decisions_block(ud.header())
    assert "사용자 결정 사항" in block
    assert "이대로 진행" in block


def test_system_prompt_format_with_confirmed_section_and_decisions():
    """v7 회귀 시나리오 그대로: 사용자 답변이 기록된 상태 + confirmed."""
    ud = UserDecisionsLog()
    ud.record("User answered — 작업 분해: 이대로 진행")
    rendered = SYSTEM_PROMPT.format(
        memory_context="",
        ledger_snapshot="ledger empty",
        decomposition_section=_build_decomposition_section(True),
        user_decisions_block=_build_user_decisions_block(ud.header()),
    )
    # 분해 확인 완료 + 사용자 답변이 둘 다 prompt 에 노출되어야 한다
    assert "분해 확인 — 완료" in rendered
    assert "이대로 진행" in rendered
    # pending 시점의 'ledger 직후 ask' 안내가 prompt 에 *남아있지* 않아야
    # 한다 — 이 회귀의 핵심.
    assert "반드시 사용자에게 분해 granularity 를" not in rendered


def test_system_prompt_format_with_pending_section_no_decisions():
    rendered = SYSTEM_PROMPT.format(
        memory_context="",
        ledger_snapshot="ledger empty",
        decomposition_section=_build_decomposition_section(False),
        user_decisions_block=_build_user_decisions_block(""),
    )
    assert "반드시 사용자에게 분해 granularity" in rendered
    assert "분해 확인 — 완료" not in rendered
