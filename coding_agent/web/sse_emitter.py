"""LangGraph 이벤트 → A2A SSE event spec 변환기.

`/a2a/stream` 핸들러가 사용하는 async generator. cli 의 `_run_agent_streaming`
패턴을 참조했으나 web 전용으로 별도 구현 — cli 회귀 위험 0.

Stream agent events emitter — LangGraph events to A2A SSE spec mapping.
Used by `/a2a/stream`. Dedicated to web (no CLI regression risk).

Emitted event names (apt-web chat UI 가 시각화하는 spec):

- ``orchestrator.run.start``       — task 시작 (session_id, request)
- ``orchestrator.run.end``         — task 종료 (success, final_response)
- ``orchestrator.role.invoke.start`` — SubAgent 위임 시작 (role, description)
- ``orchestrator.role.invoke.end``   — SubAgent 종료 (role, success, elapsed_ms)
- ``role.tool.call.start``         — 도구 호출 시작 (tool, brief)
- ``role.tool.call.end``           — 도구 호출 종료 (tool, success, output_preview)
- ``orchestrator.todo.change``     — todo 갱신 (todos)
- ``orchestrator.critic.verdict``  — sufficiency critic 판정 (band, reason)
- ``input_required``               — HITL 다중선택 질문 (question, choices)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, AsyncIterator

import structlog

# langchain / langgraph 는 함수 내부 lazy import — 모듈 자체는 의존성 없이
# 로딩 가능 (테스트가 mock AgentLoop 만 쓸 때 langgraph 미설치 환경에서도
# import 통과). Lazy import — module loads without langgraph installed
# (so unit tests using mock AgentLoop don't require the heavy deps).

log = structlog.get_logger()


# HITL 답변 대기 타임아웃 — 5분. 너무 짧으면 사용자 응답 못 따라옴.
# HITL answer wait timeout — 5 min. Too short and the user can't keep up.
_HITL_TIMEOUT_S = 300.0


def sse(event_name: str, data: dict[str, Any] | None = None) -> bytes:
    """Build a single SSE frame: ``event: NAME\\ndata: JSON\\n\\n``.

    SSE 프레임 한 개를 만든다. data 가 None 이면 빈 객체.
    """
    payload = json.dumps(data or {}, ensure_ascii=False)
    return f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8")


def _brief_from_tool_input(tool_input: Any) -> str:
    """Tool 호출 입력에서 사람이 읽을 수 있는 짧은 요약 추출.

    Best-effort — path, command, pattern, description 순서로 첫 nonempty.
    Best-effort short summary from tool input — first non-empty of common keys.
    """
    if not isinstance(tool_input, dict):
        return ""
    for key in ("path", "command", "pattern", "description", "url", "query"):
        val = tool_input.get(key)
        if val:
            s = str(val)
            return s[:80] + ("..." if len(s) > 80 else "")
    return ""


def _output_preview(output: Any) -> str:
    """Tool 결과 첫 200자만 미리보기.

    Tool output preview — first 200 chars only.
    """
    if hasattr(output, "content"):
        s = str(output.content)
    else:
        s = str(output)
    s = s.strip()
    return s[:200] + ("..." if len(s) > 200 else "")


def _is_error_output(output_str: str) -> bool:
    """간단한 에러 휴리스틱 — 결과 첫 50자 안에 'error' 단어가 있나.

    Cheap error heuristic — 'error' in first 50 chars of output.
    """
    return "error" in output_str.lower()[:50]


def _map_langgraph_event(
    kind: str,
    name: str,
    data: dict[str, Any],
    state: dict[str, Any],
) -> bytes | None:
    """LangGraph 이벤트 한 개 → SSE 프레임 (또는 None).

    state 는 generator 가 누적 추적하는 변환 상태 (subagent_depth 등).
    state is the per-stream tracking dict passed in (subagent_depth etc.).

    Map a single LangGraph event to an SSE frame, or None to skip.
    """
    # SubAgent (task tool) 위임 — orchestrator.role.invoke.* 로 변환
    # SubAgent (task tool) delegation → orchestrator.role.invoke.*
    if kind == "on_tool_start" and name == "task":
        state["subagent_depth"] += 1
        tool_input = data.get("input", {})
        if isinstance(tool_input, dict):
            raw_desc = tool_input.get("description", "")
            agent_type = tool_input.get("agent_type", "auto")
        else:
            raw_desc, agent_type = "", "auto"
        state["subagent_started_at"] = time.monotonic()
        return sse(
            "orchestrator.role.invoke.start",
            {"role": agent_type, "description": str(raw_desc)[:200]},
        )

    if kind == "on_tool_end" and name == "task":
        state["subagent_depth"] = max(0, state["subagent_depth"] - 1)
        output = data.get("output", "")
        output_str = _output_preview(output)
        success = "COMPLETED" in str(output) and "INCOMPLETE" not in str(output)
        elapsed_ms = int((time.monotonic() - state.get("subagent_started_at", 0)) * 1000)
        return sse(
            "orchestrator.role.invoke.end",
            {"role": state.get("last_role", "auto"), "success": success, "elapsed_ms": elapsed_ms},
        )

    # Inside SubAgent — suppress most events to reduce SSE noise
    # SubAgent 내부 — SSE 노이즈 줄이기 위해 대부분 이벤트 억제
    if state.get("subagent_depth", 0) > 0:
        return None

    # Top-level tool call (Orchestrator 직접 호출)
    # role.tool.call.* — note role name inferred as orchestrator-direct
    if kind == "on_tool_start":
        # write_todos 는 별도 todo.change 이벤트로 대체하므로 여기서 emit 안 함.
        # The write_todos call is surfaced as todo.change later, not here.
        if name == "write_todos":
            return None
        brief = _brief_from_tool_input(data.get("input"))
        return sse(
            "role.tool.call.start",
            {"tool": name, "brief": brief},
        )

    if kind == "on_tool_end":
        # write_todos 결과는 todo.change 로 별도 emit
        # write_todos result → emit as todo.change
        if name == "write_todos" or name == "update_todo":
            output = data.get("output", "")
            todos = _extract_todos(output)
            if todos is not None:
                return sse("orchestrator.todo.change", {"todos": todos})
            return None

        output = data.get("output", "")
        preview = _output_preview(output)
        return sse(
            "role.tool.call.end",
            {
                "tool": name,
                "success": not _is_error_output(preview),
                "output_preview": preview,
            },
        )

    # Skip everything else (chat model streaming, chain start/end internal)
    # 기타는 skip — 채팅 모델 스트리밍·내부 chain 이벤트는 너무 잦아 noise
    return None


def _extract_todos(output: Any) -> list[dict[str, Any]] | None:
    """write_todos / update_todo 도구 결과에서 todo list 추출.

    write_todos 가 ToolResult 또는 ToolMessage 를 반환한다고 가정. 정확한
    schema 모르면 best-effort 파싱.

    Best-effort todo list extraction from write_todos / update_todo result.
    Returns None when shape is unrecognized (caller skips emit).
    """
    # ToolMessage / ToolResult — content 는 보통 str 또는 list[dict].
    content = getattr(output, "content", output)

    if isinstance(content, list):
        # Already a list — may be the todo list itself.
        if all(isinstance(it, dict) for it in content):
            return [
                {
                    "id": str(it.get("id", "")),
                    "content": str(it.get("content", "")),
                    "status": str(it.get("status", "pending")),
                }
                for it in content
            ]

    if isinstance(content, str):
        # Sometimes the tool returns JSON-stringified list.
        try:
            parsed = json.loads(content)
            if isinstance(parsed, list):
                return _extract_todos(parsed)
        except (json.JSONDecodeError, TypeError):
            pass

    return None


def _extract_final_response(state: dict[str, Any] | None) -> str:
    """LangGraph 최종 state 에서 마지막 AI 메시지 본문 추출.

    Extract final response (last AI message content) from LangGraph state.
    """
    if not state:
        return ""
    values = state.values if hasattr(state, "values") else state
    if not isinstance(values, dict):
        return ""
    msgs = values.get("messages", [])
    for msg in reversed(msgs):
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip():
            return content
    return values.get("final_response", "") or ""


async def stream_agent_events(
    agent_loop: Any,
    user_message: str,
    task_id: str,
    project_id: str | None,
    pending_interrupts: dict[str, dict[str, Any]],
) -> AsyncIterator[bytes]:
    """Run AgentLoop and yield A2A SSE events.

    - Translates LangGraph events to spec event names.
    - On ask_user_question interrupt: emits ``input_required``, registers a
      ``Future`` in ``pending_interrupts[task_id]``, awaits the answer, and
      resumes the graph via ``Command(resume=answer)``.
    - On error/timeout: emits ``orchestrator.run.end`` with ``success=False``.

    AgentLoop 을 실행하면서 A2A SSE 이벤트를 yield. interrupt 발생 시
    `input_required` emit + pending_interrupts 에 Future 등록 + 답변 대기 후
    `Command(resume=answer)` 로 그래프 재개. 에러/타임아웃 시 run.end success=False.
    """
    # Lazy import — see module docstring (langgraph/langchain unavailable
    # at module load time in some test environments). ``Command`` 는 interrupt
    # 처리 직전 에 import (interrupts=None 케이스에서는 langgraph 미설치도 OK).
    from langchain_core.messages import HumanMessage

    from coding_agent.config import get_config

    started_at = time.time()

    yield sse(
        "orchestrator.run.start",
        {
            "session_id": task_id,
            "request": user_message[:500],
            "started_at": started_at,
        },
    )

    graph = agent_loop._graph
    cfg = get_config()
    config = {
        "recursion_limit": 500,
        "configurable": {"thread_id": f"a2a-{task_id}"},
    }
    initial_state = {
        "messages": [HumanMessage(content=user_message)],
        "project_id": project_id or cfg.project_id or "",
        "working_directory": os.getcwd(),
    }

    # Per-stream mapping state — subagent depth, last role, etc.
    map_state: dict[str, Any] = {
        "subagent_depth": 0,
        "subagent_started_at": 0.0,
        "last_role": "auto",
    }

    next_input: Any = initial_state
    success = True
    error_msg = ""

    try:
        while True:
            async for event in graph.astream_events(
                next_input, version="v2", config=config
            ):
                kind = event.get("event", "")
                name = event.get("name", "")
                data = event.get("data", {})

                frame = _map_langgraph_event(kind, name, data, map_state)
                if frame:
                    yield frame

            # Stream ended — check for interrupt
            # 스트림 끝 — interrupt 검사
            snap = None
            try:
                snap = await graph.aget_state(config)
            except Exception as exc:
                log.debug("a2a.stream.get_state.error", error=str(exc))

            interrupts = getattr(snap, "interrupts", None) if snap else None
            if not interrupts:
                break

            payload = getattr(interrupts[0], "value", None)
            if not (isinstance(payload, dict) and payload.get("kind") == "ask_user_question"):
                # Unknown interrupt — surface and bail
                # 모르는 interrupt — 표면화 후 종료
                success = False
                error_msg = f"unhandled interrupt: {type(payload).__name__}"
                break

            # HITL — register future + emit input_required + await
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            pending_interrupts[task_id] = {
                "future": future,
                "thread_id": config["configurable"]["thread_id"],
            }

            yield sse(
                "input_required",
                {
                    "session_id": task_id,
                    "question": payload.get("question", "추가 결정이 필요합니다"),
                    "choices": payload.get("choices", []),
                    "allow_free_text": payload.get("allow_free_text", False),
                },
            )

            try:
                answer = await asyncio.wait_for(future, timeout=_HITL_TIMEOUT_S)
            except asyncio.TimeoutError:
                pending_interrupts.pop(task_id, None)
                success = False
                error_msg = "hitl timeout (5min)"
                break
            finally:
                pending_interrupts.pop(task_id, None)

            # Resume graph — Command import 도 lazy (interrupt 케이스만 사용).
            from langgraph.types import Command  # noqa: WPS433
            next_input = Command(resume=answer)
            # loop continues — astream_events again with resume input

    except Exception as exc:
        log.error("a2a.stream.error", task_id=task_id, error=str(exc))
        success = False
        error_msg = f"{type(exc).__name__}: {exc}"

    # Final response from graph state
    final_response = ""
    try:
        final_state = await graph.aget_state(config)
        final_response = _extract_final_response(final_state)
    except Exception as exc:
        log.debug("a2a.stream.final_state.error", error=str(exc))

    yield sse(
        "orchestrator.run.end",
        {
            "session_id": task_id,
            "success": success,
            "final_response": final_response,
            "error": error_msg if not success else None,
            "elapsed_s": round(time.time() - started_at, 2),
        },
    )


__all__ = ["sse", "stream_agent_events"]
