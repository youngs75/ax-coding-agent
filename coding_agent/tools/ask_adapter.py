"""``ask_user_question`` — minyoung_mah.ToolAdapter 변형.

Plan §결정 3: role 내부에서 langgraph ``interrupt()`` 를 직접 호출하면 library
loop 를 가로질러 올라가기 때문에, SubAgent 경로에서는 대신 ``ToolResult`` 에
:data:`minyoung_mah.HITL_INTERRUPT_MARKER` 마커를 넣어 돌려준다. role
system_prompt 가 이 마커를 보면 즉시 짧은 요약으로 마무리하도록 유도해,
task_tool 레이어에서 interrupt 를 잡아 LangGraph 최상위로 propagate 한다
(Phase 6).
"""

from __future__ import annotations

import time

from minyoung_mah import (
    ToolResult,
    extract_interrupt_payload,
    make_interrupt_marker,
)

from coding_agent.tools.ask_tool import (
    AskQuestionItem,
    AskUserQuestionInput,
    _build_payload,
)


class AskUserQuestionAdapter:
    """SubAgent 경로용 ``ask_user_question`` 어댑터.

    LangGraph ``interrupt()`` 대신 HITL 마커 envelope 을 리턴한다. task_tool
    이 role COMPLETED 후 ``result.tool_results`` 를 훑어 이 마커를 찾고
    최상위 LangGraph 에서 실제 ``interrupt(payload)`` 를 호출한다.
    """

    name: str = "ask_user_question"
    description: str = (
        "Pause and ask the user 1–4 multiple-choice questions about essential "
        "decisions."
    )
    arg_schema: type = AskUserQuestionInput

    async def call(self, args: AskUserQuestionInput) -> ToolResult:
        t0 = time.monotonic()
        try:
            questions: list[AskQuestionItem] = list(args.questions)
            payload = _build_payload(questions)
            duration_ms = int((time.monotonic() - t0) * 1000)
            return ToolResult(
                ok=True,
                value=make_interrupt_marker(payload),
                duration_ms=duration_ms,
            )
        except Exception as exc:  # noqa: BLE001
            duration_ms = int((time.monotonic() - t0) * 1000)
            return ToolResult(
                ok=False,
                value=None,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=duration_ms,
            )


ask_user_question_adapter = AskUserQuestionAdapter()


__all__ = [
    "AskUserQuestionAdapter",
    "ask_user_question_adapter",
    "extract_interrupt_payload",
]
