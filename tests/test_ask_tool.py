"""Tests for ask_user_question tool — schema, payload, formatting, renderer."""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from rich.console import Console

from coding_agent.tools.ask_tool import (
    AskQuestionItem,
    AskQuestionOption,
    AskUserQuestionInput,
    _build_payload,
    _format_answer,
    ask_user_question_tool,
)
from coding_agent.cli.question_renderer import render_ask_user_question


# ── Schema validation ──────────────────────────────────────────────


def _q(header="Tech", question="Pick stack?", options=None, **kw) -> dict[str, Any]:
    return {
        "header": header,
        "question": question,
        "options": options or [
            {"label": "FastAPI", "description": "py"},
            {"label": "Nest", "description": "ts"},
        ],
        **kw,
    }


def test_schema_accepts_minimal_question():
    AskUserQuestionInput.model_validate({"questions": [_q()]})


def test_schema_rejects_too_many_questions():
    with pytest.raises(Exception):
        AskUserQuestionInput.model_validate(
            {"questions": [_q(header=f"H{i}") for i in range(5)]}
        )


def test_schema_rejects_too_few_options():
    bad = _q(options=[{"label": "only one"}])
    with pytest.raises(Exception):
        AskUserQuestionInput.model_validate({"questions": [bad]})


def test_schema_rejects_too_long_header():
    with pytest.raises(Exception):
        AskUserQuestionInput.model_validate(
            {"questions": [_q(header="THIS_IS_WAY_TOO_LONG")]}
        )


def test_schema_rejects_duplicate_headers():
    with pytest.raises(Exception):
        AskUserQuestionInput.model_validate(
            {"questions": [_q(header="X"), _q(header="X")]}
        )


# ── Payload + answer formatting ────────────────────────────────────


def test_build_payload_shape():
    items = [
        AskQuestionItem(
            header="Tech",
            question="Pick?",
            options=[
                AskQuestionOption(label="A", description="a"),
                AskQuestionOption(label="B", description="b"),
            ],
        )
    ]
    p = _build_payload(items)
    assert p["kind"] == "ask_user_question"
    assert len(p["questions"]) == 1
    assert p["questions"][0]["header"] == "Tech"
    assert p["questions"][0]["options"][0]["label"] == "A"


def test_format_answer_single():
    payload = {
        "questions": [
            {"header": "Tech", "question": "?", "options": [], "multi_select": False}
        ]
    }
    out = _format_answer(payload, {"Tech": "FastAPI"})
    assert "Tech: FastAPI" in out
    assert out.startswith("User answered")


def test_format_answer_multi():
    payload = {
        "questions": [
            {"header": "Plat", "question": "?", "options": [], "multi_select": True}
        ]
    }
    out = _format_answer(payload, {"Plat": ["iOS", "Android"]})
    assert "iOS, Android" in out


def test_format_answer_skipped():
    payload = {
        "questions": [
            {"header": "Auth", "question": "?", "options": [], "multi_select": False}
        ]
    }
    out = _format_answer(payload, {})
    assert "Auth: (skipped)" in out


# ── CLI renderer with stub input ───────────────────────────────────


def _make_input_stub(*replies: str):
    """Return a callable that pops one reply per call."""
    queue = list(replies)

    def _stub(prompt: str = "") -> str:
        return queue.pop(0) if queue else ""

    return _stub


def _silent_console() -> Console:
    return Console(file=StringIO(), force_terminal=False)


def test_renderer_single_select_picks_label():
    payload = _build_payload([
        AskQuestionItem(
            header="Tech",
            question="Pick stack?",
            options=[
                AskQuestionOption(label="FastAPI", description=""),
                AskQuestionOption(label="Nest", description=""),
            ],
        )
    ])
    answers = render_ask_user_question(
        payload,
        console=_silent_console(),
        input_fn=_make_input_stub("1"),
    )
    assert answers == {"Tech": "FastAPI"}


def test_renderer_multi_select_returns_list():
    payload = _build_payload([
        AskQuestionItem(
            header="Plat",
            question="Pick platforms?",
            multi_select=True,
            options=[
                AskQuestionOption(label="iOS", description=""),
                AskQuestionOption(label="Android", description=""),
                AskQuestionOption(label="Web", description=""),
            ],
        )
    ])
    answers = render_ask_user_question(
        payload,
        console=_silent_console(),
        input_fn=_make_input_stub("1,3"),
    )
    assert answers == {"Plat": ["iOS", "Web"]}


def test_renderer_skip_returns_no_key():
    payload = _build_payload([
        AskQuestionItem(
            header="Auth",
            question="Pick auth?",
            options=[
                AskQuestionOption(label="JWT", description=""),
                AskQuestionOption(label="Session", description=""),
            ],
        )
    ])
    answers = render_ask_user_question(
        payload,
        console=_silent_console(),
        input_fn=_make_input_stub("/skip"),
    )
    assert answers == {}


def test_renderer_other_free_form():
    payload = _build_payload([
        AskQuestionItem(
            header="DB",
            question="DB?",
            options=[
                AskQuestionOption(label="Postgres", description=""),
                AskQuestionOption(label="MySQL", description=""),
            ],
        )
    ])
    # 3 = "type your own", then the free-form answer
    answers = render_ask_user_question(
        payload,
        console=_silent_console(),
        input_fn=_make_input_stub("3", "SQLite"),
    )
    assert answers == {"DB": "SQLite"}


def test_renderer_invalid_then_valid():
    payload = _build_payload([
        AskQuestionItem(
            header="Tech",
            question="?",
            options=[
                AskQuestionOption(label="A", description=""),
                AskQuestionOption(label="B", description=""),
            ],
        )
    ])
    # First an invalid number, then a valid one
    answers = render_ask_user_question(
        payload,
        console=_silent_console(),
        input_fn=_make_input_stub("99", "2"),
    )
    assert answers == {"Tech": "B"}


# ── Tool registration sanity ───────────────────────────────────────


def test_ask_adapter_registered_in_orchestrator_tool_registry():
    """Phase 6: ask_user_question 은 minyoung_mah.ToolAdapter 로 등록되어야
    SubAgent role 들이 bind_tools 에서 실제로 볼 수 있다."""
    from minyoung_mah import SqliteMemoryStore

    from coding_agent.subagents.orchestrator_factory import build_orchestrator
    from coding_agent.subagents.user_decisions import UserDecisionsLog

    store = SqliteMemoryStore(":memory:", tiers=["user", "project", "domain"])
    orch = build_orchestrator(memory_store=store, user_decisions=UserDecisionsLog())
    assert "ask_user_question" in orch.tools.names()


def test_planner_role_has_ask_user_question():
    from coding_agent.subagents.roles import planner_role

    role = planner_role()
    assert "ask_user_question" in role.tool_allowlist
