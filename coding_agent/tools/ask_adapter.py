"""``ask_user_question`` — minyoung_mah.ToolAdapter 변형.

Plan §결정 3: role 내부에서 langgraph ``interrupt()`` 를 직접 호출하면 library
loop 를 가로질러 올라가기 때문에, SubAgent 경로에서는 대신 ``ToolResult`` 에
``__ax_interrupt__`` 마커를 넣어 돌려준다. role system_prompt 가 이 마커를 보면
즉시 짧은 요약으로 마무리하도록 유도해, task_tool 레이어에서 interrupt 를
잡아 LangGraph 최상위로 propagate 한다 (Phase 6).
"""

from __future__ import annotations

import time
from typing import Any

from minyoung_mah.core.types import ToolResult

from coding_agent.tools.ask_tool import (
    AskQuestionItem,
    AskUserQuestionInput,
    _build_payload,
)


class AskUserQuestionAdapter:
    """SubAgent 경로용 ``ask_user_question`` 어댑터.

    LangGraph ``interrupt()`` 대신 ``__ax_interrupt__`` 마커 dict 를 리턴한다.
    task_tool 이 role COMPLETED 후 ``result.tool_results`` 를 훑어 이 마커를
    찾고 최상위 LangGraph 에서 실제 ``interrupt(payload)`` 를 호출한다.
    """

    name: str = "ask_user_question"
    description: str = (
        "Pause and ask the user 1–4 multiple-choice questions about essential "
        "decisions. 결과가 __ax_interrupt__ 를 포함하면 즉시 짧은 요약 한 줄로 "
        "응답을 마치세요 (상위 레이어가 사용자 답변 후 다시 호출합니다)."
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
                value={"__ax_interrupt__": True, "payload": payload},
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


def extract_interrupt_payload(tool_result_value: Any) -> dict[str, Any] | None:
    """Detect the ``__ax_interrupt__`` marker inside a serialized tool result.

    The Orchestrator serializes ToolResult.value via ``json.dumps`` before
    feeding it to the LLM (see minyoung_mah.core.orchestrator._serialize_tool_value).
    task_tool calls this on the *original* ``ToolResult.value`` dict to
    recover the payload dict for downstream ``interrupt()``.
    """
    if isinstance(tool_result_value, dict) and tool_result_value.get("__ax_interrupt__"):
        return tool_result_value.get("payload")
    return None


__all__ = [
    "AskUserQuestionAdapter",
    "ask_user_question_adapter",
    "extract_interrupt_payload",
]
