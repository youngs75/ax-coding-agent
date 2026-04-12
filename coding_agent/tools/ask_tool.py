"""Interactive clarification tool — ``ask_user_question``.

Pattern borrowed from Claude Code's AskUserQuestionTool. The LLM bundles
1–4 related questions, each with 2–4 multiple-choice options, and the
harness pauses graph execution via ``langgraph.types.interrupt()`` until
the user picks an answer. The CLI is responsible for rendering the
questions and supplying answers when it sees ``__interrupt__`` in the
graph state.

Why this exists
---------------
Vague user requirements ("build a PMS") cause the planner to invent
features the user never asked for, producing a 6× over-specced PRD.
Letting the planner ask first — "which tech stack? mobile or web only?
auth scope?" — replaces invented assumptions with user-verified facts.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from langgraph.types import interrupt
from pydantic import BaseModel, Field, field_validator


class AskQuestionOption(BaseModel):
    label: str = Field(
        description="Short label shown to the user (max ~40 chars).",
        min_length=1,
        max_length=120,
    )
    description: str = Field(
        default="",
        description="One- or two-sentence explanation of this option.",
        max_length=400,
    )


class AskQuestionItem(BaseModel):
    question: str = Field(
        description="The question text. Should end with '?'.",
        min_length=1,
        max_length=400,
    )
    header: str = Field(
        description="Short tab label (≤12 chars) for the progress bar.",
        min_length=1,
        max_length=12,
    )
    options: list[AskQuestionOption] = Field(
        description="2–4 mutually exclusive choices.",
        min_length=2,
        max_length=4,
    )
    multi_select: bool = Field(
        default=False,
        description="If true, the user can pick more than one option.",
    )
    allow_other: bool = Field(
        default=True,
        description="If true, the user can type a free-form answer instead of picking an option.",
    )


class AskUserQuestionInput(BaseModel):
    """Input schema for ``ask_user_question``."""

    questions: list[AskQuestionItem] = Field(
        description=(
            "1 to 4 related questions to bundle in a single user prompt. "
            "Use this BEFORE writing PRD/SPEC when the user request leaves "
            "essential decisions unspecified (tech stack, auth scope, "
            "platform targets, etc.). Do NOT use it for trivia or yes/no "
            "questions you can decide yourself."
        ),
        min_length=1,
        max_length=4,
    )

    @field_validator("questions")
    @classmethod
    def _validate_headers_unique(cls, value):
        seen: set[str] = set()
        for q in value:
            if q.header in seen:
                raise ValueError(
                    f"duplicate header '{q.header}' — each question needs a unique tab label"
                )
            seen.add(q.header)
        return value


def _build_payload(questions: list[AskQuestionItem]) -> dict[str, Any]:
    """Convert the validated input into the dict that the CLI renders."""
    return {
        "kind": "ask_user_question",
        "questions": [
            {
                "header": q.header,
                "question": q.question,
                "multi_select": q.multi_select,
                "allow_other": q.allow_other,
                "options": [
                    {"label": o.label, "description": o.description}
                    for o in q.options
                ],
            }
            for q in questions
        ],
    }


def _format_answer(payload: dict[str, Any], answer: Any) -> str:
    """Format the user's answer for the LLM tool result.

    Returns a compact, deterministic string so the planner can read it
    and continue. Multi-select answers are rendered as comma-separated
    lists. Free-form answers are wrapped in quotes.
    """
    if not isinstance(answer, dict):
        # Defensive: caller passed a flat answer for a single question
        return f"User answered: {answer}"

    parts: list[str] = []
    for q in payload.get("questions", []):
        header = q["header"]
        a = answer.get(header)
        if a is None:
            parts.append(f"{header}: (skipped)")
        elif isinstance(a, list):
            parts.append(f"{header}: {', '.join(str(x) for x in a)}")
        else:
            parts.append(f"{header}: {a}")
    return "User answered — " + " | ".join(parts)


def _ask_user_question(questions: list[AskQuestionItem]) -> str:
    """Pause the graph and surface the questions to the CLI.

    The first call raises ``GraphInterrupt`` (via langgraph's ``interrupt``)
    so the orchestrator/planner pauses. After the user answers, the
    second call returns the answer immediately.
    """
    payload = _build_payload(questions)
    answer = interrupt(payload)
    return _format_answer(payload, answer)


ask_user_question_tool = StructuredTool.from_function(
    func=_ask_user_question,
    name="ask_user_question",
    description=(
        "Pause work and ask the user 1–4 multiple-choice questions about "
        "essential decisions you cannot infer from the request "
        "(e.g. tech stack, auth scope, mobile vs web, library choice). "
        "Use BEFORE writing PRD/SPEC when the request is vague. "
        "Do NOT use for trivial yes/no questions or things you can decide. "
        "LANGUAGE: question text, header labels, option labels, and option "
        "descriptions MUST be written in Korean unless the user explicitly "
        "requested another language. The user reads these directly. "
        "The harness blocks until the user answers and returns their "
        "selections as a single string formatted: "
        "'User answered — Header1: choice | Header2: choice ...'."
    ),
    args_schema=AskUserQuestionInput,
)


__all__ = [
    "AskQuestionItem",
    "AskQuestionOption",
    "AskUserQuestionInput",
    "ask_user_question_tool",
    "_build_payload",
    "_format_answer",
]
