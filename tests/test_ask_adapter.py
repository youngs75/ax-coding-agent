"""AskUserQuestionAdapter — HITL interrupt marker 경로 검증 (plan §결정 3).

Role 내부에서 실제 langgraph ``interrupt()`` 를 호출하지 않고
:data:`minyoung_mah.HITL_INTERRUPT_MARKER` 마커 envelope 만 리턴하는지,
task_tool 이 그 마커를 detect 할 수 있는지 검증.
"""

from __future__ import annotations

import pytest
from minyoung_mah import HITL_INTERRUPT_MARKER

from coding_agent.tools.ask_adapter import (
    ask_user_question_adapter,
    extract_interrupt_payload,
)
from coding_agent.tools.ask_tool import AskQuestionItem, AskQuestionOption, AskUserQuestionInput


def _make_input() -> AskUserQuestionInput:
    return AskUserQuestionInput(
        questions=[
            AskQuestionItem(
                question="어떤 스택을 쓸까요?",
                header="Tech",
                options=[
                    AskQuestionOption(label="React"),
                    AskQuestionOption(label="Vue"),
                ],
            )
        ]
    )


@pytest.mark.asyncio
async def test_adapter_returns_interrupt_marker_instead_of_raising():
    result = await ask_user_question_adapter.call(_make_input())
    assert result.ok is True
    assert isinstance(result.value, dict)
    assert result.value.get(HITL_INTERRUPT_MARKER) is True
    assert "payload" in result.value


@pytest.mark.asyncio
async def test_adapter_payload_shape_matches_cli_expectation():
    result = await ask_user_question_adapter.call(_make_input())
    payload = result.value["payload"]
    assert payload["kind"] == "ask_user_question"
    assert isinstance(payload["questions"], list)
    q = payload["questions"][0]
    assert q["header"] == "Tech"
    assert any(opt["label"] == "React" for opt in q["options"])


def test_extract_interrupt_payload_recovers_dict():
    value = {HITL_INTERRUPT_MARKER: True, "payload": {"kind": "ask_user_question"}}
    assert extract_interrupt_payload(value) == {"kind": "ask_user_question"}


def test_extract_interrupt_payload_returns_none_for_plain_strings():
    assert extract_interrupt_payload("User answered — Tech: React") is None
    assert extract_interrupt_payload({"other": "value"}) is None
    assert extract_interrupt_payload(None) is None


def test_adapter_is_a_toolAdapter_protocol_duck_type():
    from minyoung_mah import ToolAdapter

    assert isinstance(ask_user_question_adapter, ToolAdapter)
    assert ask_user_question_adapter.name == "ask_user_question"
    assert ask_user_question_adapter.arg_schema is AskUserQuestionInput
