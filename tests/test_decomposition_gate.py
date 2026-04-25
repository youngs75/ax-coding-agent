"""Decomposition gate — ledger 등록 후 사용자 granularity 확인 강제.

v2 E2E (2026-04-22): planner 가 "백엔드 API 개발" 같은 거대 묶음 8개로
분해하고 orchestrator 가 사용자 승인 없이 바로 coder 로 돌진 → coder 가
"완료" 기준을 자의적으로 판단. v1 은 반대로 46 tasks 로 과도 세분화.
어느 쪽이든 한 번은 사용자가 보고 조정할 수 있는 게이트가 필요.

v6~v9 회귀 분석 (2026-04-25): prompt-fidelity 의존 게이트 (orchestrator 에게
``ask_user_question`` 직접 호출하라고 SYSTEM_PROMPT 로 지시) 가 모델 순종도에
따라 깨짐. 재설계: harness 가 ``interrupt()`` 로 직접 묻고 답변에 따라
state mutation 을 결정한다. SYSTEM_PROMPT 의 "## 분해 확인" 섹션은 삭제됨.

여기서는 pure 함수 4 개를 검증한다 (그래프는 세우지 않음):
- ``_requires_decomposition_gate`` (게이트 진입 조건; 기존)
- ``_build_decomposition_interrupt_payload`` (interrupt payload 빌드; 신규)
- ``_extract_decomposition_answer`` (resume 답변 파싱; 신규)
- ``_classify_decomposition_answer`` (proceed/finer/coarser/unknown 분류; 신규)
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from coding_agent.core.loop import (
    SYSTEM_PROMPT,
    _build_decomposition_interrupt_payload,
    _classify_decomposition_answer,
    _extract_decomposition_answer,
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


# ── _build_decomposition_interrupt_payload ──────────────────────────────────


def test_payload_has_ask_user_question_shape():
    # The payload must mirror the ``ask_user_question`` tool format so the
    # CLI's question_renderer can handle both code paths uniformly.
    counts = {"pending": 8, "in_progress": 0, "completed": 0}
    preview = ["TASK-01: impl A", "TASK-02: impl B"]
    payload = _build_decomposition_interrupt_payload(counts, preview)

    assert payload["kind"] == "ask_user_question"
    assert isinstance(payload["questions"], list)
    assert len(payload["questions"]) == 1
    q = payload["questions"][0]
    assert q["header"] == "분해 확인"
    assert q["multi_select"] is False
    # Three options: 이대로 진행 / 더 세분화 / 더 통합
    labels = [opt["label"] for opt in q["options"]]
    assert labels == ["이대로 진행", "더 세분화", "더 통합"]


def test_payload_includes_preview_and_count():
    counts = {"pending": 8, "in_progress": 0, "completed": 0}
    preview = [
        "TASK-01: 사용자 인증",
        "TASK-02: 아키텍처 설계",
        "TASK-03: DB 스키마",
    ]
    payload = _build_decomposition_interrupt_payload(counts, preview)
    text = payload["questions"][0]["question"]
    assert "TASK-01" in text
    assert "TASK-02" in text
    assert "TASK-03" in text
    assert "8" in text


def test_payload_advisory_when_too_many():
    # total > 15 → "세분화가 과한 것 같습니다." 권고
    counts = {"pending": 46, "in_progress": 0, "completed": 0}
    preview = [f"TASK-{i:02d}: item" for i in range(1, 6)]
    payload = _build_decomposition_interrupt_payload(counts, preview)
    text = payload["questions"][0]["question"]
    assert "세분화가 과한" in text
    assert "5~15" in text
    # Overflow hint: 46 - 5 shown = 41 remaining.
    assert "외 41개" in text


def test_payload_advisory_when_too_few():
    # total < 4 → "통합이 과한 것 같습니다." 권고
    counts = {"pending": 2, "in_progress": 0, "completed": 0}
    preview = ["TASK-01: a", "TASK-02: b"]
    payload = _build_decomposition_interrupt_payload(counts, preview)
    text = payload["questions"][0]["question"]
    assert "통합이 과한" in text
    assert "5~15" in text


def test_payload_no_advisory_in_sweet_spot():
    counts = {"pending": 8, "in_progress": 0, "completed": 0}
    preview = [f"TASK-{i:02d}: a" for i in range(1, 6)]
    payload = _build_decomposition_interrupt_payload(counts, preview)
    text = payload["questions"][0]["question"]
    assert "세분화가 과한" not in text
    assert "통합이 과한" not in text


def test_payload_no_overflow_when_preview_matches_total():
    counts = {"pending": 3, "in_progress": 0, "completed": 0}
    preview = ["TASK-01: a", "TASK-02: b", "TASK-03: c"]
    payload = _build_decomposition_interrupt_payload(counts, preview)
    text = payload["questions"][0]["question"]
    assert "외 " not in text  # no overflow hint when preview covers all items


# ── _extract_decomposition_answer ───────────────────────────────────────────


def test_extract_answer_dict_keyed_by_header():
    # CLI ``render_ask_user_question`` returns dict keyed by question header.
    answer = {"분해 확인": "이대로 진행"}
    assert _extract_decomposition_answer(answer) == "이대로 진행"


def test_extract_answer_dict_first_value_fallback():
    # Header missing — fall back to the first value.
    answer = {"otherKey": "더 세분화"}
    assert _extract_decomposition_answer(answer) == "더 세분화"


def test_extract_answer_dict_with_list_value():
    # Multi-select would return a list; we take the first.
    answer = {"분해 확인": ["더 통합"]}
    assert _extract_decomposition_answer(answer) == "더 통합"


def test_extract_answer_list_of_dicts():
    # Programmatic resume (tests, alternate hosts) may pass list-of-dict.
    answer = [{"header": "분해 확인", "value": "이대로 진행"}]
    assert _extract_decomposition_answer(answer) == "이대로 진행"


def test_extract_answer_list_of_dicts_with_answer_key():
    answer = [{"header": "분해 확인", "answer": "더 세분화"}]
    assert _extract_decomposition_answer(answer) == "더 세분화"


def test_extract_answer_bare_string():
    assert _extract_decomposition_answer("이대로 진행") == "이대로 진행"


def test_extract_answer_empty_returns_empty_string():
    assert _extract_decomposition_answer(None) == ""
    assert _extract_decomposition_answer({}) == ""
    assert _extract_decomposition_answer([]) == ""
    assert _extract_decomposition_answer("") == ""


def test_extract_answer_strips_whitespace():
    assert _extract_decomposition_answer("  이대로 진행  ") == "이대로 진행"


# ── _classify_decomposition_answer ──────────────────────────────────────────


def test_classify_proceed_label():
    assert _classify_decomposition_answer("이대로 진행") == "proceed"


def test_classify_proceed_paraphrase():
    assert _classify_decomposition_answer("그대로 가시죠") == "proceed"
    assert _classify_decomposition_answer("진행해주세요") == "proceed"


def test_classify_finer():
    assert _classify_decomposition_answer("더 세분화") == "finer"
    assert _classify_decomposition_answer("좀 더 세분화해주세요") == "finer"


def test_classify_coarser():
    assert _classify_decomposition_answer("더 통합") == "coarser"
    assert _classify_decomposition_answer("통합이 필요합니다") == "coarser"


def test_classify_unknown_for_blank():
    assert _classify_decomposition_answer("") == "unknown"


def test_classify_unknown_for_unrelated_text():
    assert _classify_decomposition_answer("나중에 결정할게요") == "unknown"


# ── SYSTEM_PROMPT regression: legacy "분해 확인" section removed ────────────


def test_system_prompt_no_decomposition_section():
    # The "## 분해 확인" prompt instructions were removed when the gate
    # moved into the harness. Ensure no stale text leaks back in.
    assert "## 분해 확인" not in SYSTEM_PROMPT
    # Decomposition-specific labels also gone (planner-side ask_user_question
    # for requirement clarification is unrelated and may still be referenced).
    assert "더 세분화" not in SYSTEM_PROMPT
    assert "더 통합" not in SYSTEM_PROMPT
    assert "이대로 진행" not in SYSTEM_PROMPT


def test_system_prompt_no_decomposition_placeholder():
    # Legacy ``{decomposition_section}`` placeholder must not be present
    # — the format() call in agent_node does not pass a value for it.
    assert "{decomposition_section}" not in SYSTEM_PROMPT


def test_system_prompt_format_succeeds_with_current_args():
    # agent_node calls SYSTEM_PROMPT.format(memory_context=..., ledger_snapshot=...)
    # — verify those are still the only required placeholders.
    rendered = SYSTEM_PROMPT.format(memory_context="m", ledger_snapshot="l")
    assert "m" in rendered
    assert "l" in rendered
