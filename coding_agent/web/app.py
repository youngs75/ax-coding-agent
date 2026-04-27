"""FastAPI daemon entry for ax-coding-agent.

A2A endpoint 8 종을 등록하고, AgentLoop 오케스트레이터를 실제로 호출한다.
tasks/send 는 동기 JSON 응답, stream 은 SSE, respond 는 HITL interrupt 재개.

A2A 엔드포인트에서 AgentLoop.run() 을 직접 호출하여 LangGraph 기반
멀티 에이전트 오케스트레이터가 코드 작성·검증을 수행한다.

Routes:

- ``GET  /healthz``                  — liveness probe.
- ``GET  /.well-known/agent.json``   — A2A agent card (dynamic host).
- ``POST /a2a/tasks/send``           — sync JSON-RPC task.
- ``POST /a2a/stream``               — SSE streaming task.
- ``POST /a2a/respond``              — HITL response submission.
- ``POST /a2a``                      — portal probe fallback → tasks/send.
- ``POST /a2a/jsonrpc``              — portal probe fallback → tasks/send.
- ``POST /a2a/rest``                 — portal probe fallback → tasks/send.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .agent_card import _resolve_version, build_agent_card
from .sse_emitter import stream_agent_events

log = structlog.get_logger()

__version__ = _resolve_version()

# ---------------------------------------------------------------------------
# AgentLoop singleton — created once at startup, shared across requests.
# AgentLoop 싱글톤 — startup 시 한 번 생성, 요청 간 공유.
# ---------------------------------------------------------------------------

_agent_loop = None

# HITL interrupt 대기 중인 task 저장 {task_id: {future, thread_id, ...}}
_pending_interrupts: dict[str, dict[str, Any]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent_loop
    from coding_agent.core.loop import AgentLoop

    log.info("app.startup", msg="Initializing AgentLoop")
    _agent_loop = AgentLoop()
    log.info("app.startup.done")
    yield
    if _agent_loop is not None:
        _agent_loop.close()
        log.info("app.shutdown", msg="AgentLoop closed")


app = FastAPI(title="ax-coding-agent", version=__version__, lifespan=lifespan)


# ---------------------------------------------------------------------------
# Liveness / agent card
# ---------------------------------------------------------------------------


@app.get("/")
@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """K8s liveness probe. ``/`` 는 ALB/포털 default health check 용."""
    return {"status": "ok", "version": __version__}


@app.get("/.well-known/agent.json")
async def well_known_agent(request: Request) -> dict[str, Any]:
    """Dynamic A2A agent card."""
    return build_agent_card(request)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_message(body: dict[str, Any]) -> str:
    """Extract user message from A2A request body.

    Supports both flat ``{"message": "..."}`` and nested
    ``{"params": {"message": {"parts": [{"text": "..."}]}}}`` (A2A spec).
    """
    if "message" in body and isinstance(body["message"], str):
        return body["message"]

    params = body.get("params", {})
    msg = params.get("message", {})
    if isinstance(msg, str):
        return msg

    parts = msg.get("parts", [])
    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type", "text") == "text"]
    if texts:
        return "\n".join(texts)

    if "content" in body:
        return str(body["content"])

    return json.dumps(body)


def _extract_session_id(body: dict[str, Any]) -> str | None:
    """Extract conversation session id from A2A request body.

    apt-web sends the session id under ``params.metadata.session_id`` (a
    localStorage-backed value that stays the same across turns of the same
    coding conversation). Falls back to top-level ``session_id`` / ``id``.

    apt-web 은 같은 코딩 대화의 모든 turn 동안 유지되는 session_id 를
    ``params.metadata.session_id`` 로 보낸다 (localStorage 기반). top-level
    ``session_id`` / ``id`` 도 fallback 으로 받는다.
    """
    params = body.get("params", {})
    if isinstance(params, dict):
        meta = params.get("metadata", {})
        if isinstance(meta, dict):
            sid = meta.get("session_id")
            if sid:
                return str(sid)
    sid = body.get("session_id") or body.get("id")
    if sid:
        return str(sid)
    return None


def _thread_id_for(session_id: str | None, task_id: str) -> str:
    """Build LangGraph checkpointer thread_id from session_id (preferred)
    or fall back to per-call task_id (legacy behaviour).

    같은 session_id 의 turn 들은 같은 thread 에서 conversation state 누적;
    session_id 가 없으면 task_id 기반으로 turn 독립.
    """
    if session_id:
        return f"a2a-{session_id}"
    return f"a2a-{task_id}"


def _build_a2a_response(task_id: str, state: dict[str, Any]) -> dict[str, Any]:
    """Build A2A-compliant JSON-RPC response from AgentLoop final state."""
    final_response = state.get("final_response", "")
    exit_reason = state.get("exit_reason", "completed")

    a2a_status = "completed"
    if exit_reason == "fatal_error":
        a2a_status = "failed"
    elif exit_reason == "no_ask_user_handler":
        a2a_status = "input-required"
    elif state.get("__interrupt__"):
        a2a_status = "input-required"

    result: dict[str, Any] = {
        "id": task_id,
        "status": {"state": a2a_status},
        "artifacts": [
            {
                "parts": [{"type": "text", "text": final_response}],
            }
        ],
    }

    if exit_reason:
        result["metadata"] = {
            "exit_reason": exit_reason,
            "iterations": state.get("iteration", 0),
        }

    return result


# ---------------------------------------------------------------------------
# A2A — synchronous tasks/send (+ portal probe fallbacks)
# ---------------------------------------------------------------------------


async def _handle_send(request: Request) -> JSONResponse:
    """Synchronous task handler — runs AgentLoop.run() to completion."""
    task_id = str(uuid.uuid4())

    try:
        body = await request.json()
    except Exception:
        body = {}

    user_message = _extract_message(body)
    if not user_message.strip():
        return JSONResponse(
            {"id": task_id, "status": {"state": "failed"}, "error": "empty message"},
            status_code=400,
        )

    project_id = body.get("project_id") or body.get("params", {}).get("project_id")
    session_id = _extract_session_id(body)
    thread_id = _thread_id_for(session_id, task_id)

    log.info(
        "a2a.tasks_send",
        task_id=task_id,
        session_id=session_id,
        thread_id=thread_id,
        message_length=len(user_message),
    )

    try:
        state = await _agent_loop.run(
            user_message=user_message,
            project_id=project_id,
            thread_id=thread_id,
        )
        return JSONResponse(_build_a2a_response(task_id, state))
    except Exception as e:
        log.error("a2a.tasks_send.error", task_id=task_id, error=str(e))
        return JSONResponse(
            {
                "id": task_id,
                "status": {"state": "failed"},
                "error": str(e),
            },
            status_code=500,
        )


app.post("/a2a/tasks/send")(_handle_send)
app.post("/a2a")(_handle_send)
app.post("/a2a/jsonrpc")(_handle_send)
app.post("/a2a/rest")(_handle_send)


# ---------------------------------------------------------------------------
# A2A — streaming task (SSE)
# ---------------------------------------------------------------------------


@app.post("/a2a/stream")
async def tasks_stream(request: Request) -> StreamingResponse:
    """SSE stream — runs the orchestrator and emits A2A spec events.

    Emits the rich event spec used by apt-web chat UI:

    - ``orchestrator.run.start`` / ``orchestrator.run.end``
    - ``orchestrator.role.invoke.start`` / ``orchestrator.role.invoke.end``
    - ``role.tool.call.start`` / ``role.tool.call.end``
    - ``orchestrator.todo.change``
    - ``input_required`` (HITL — paired with ``POST /a2a/respond``)

    Mapping is in :mod:`coding_agent.web.sse_emitter`.
    """
    task_id = str(uuid.uuid4())

    try:
        body = await request.json()
    except Exception:
        body = {}

    user_message = _extract_message(body)
    project_id = body.get("project_id") or body.get("params", {}).get("project_id")
    session_id = _extract_session_id(body)

    log.info(
        "a2a.stream",
        task_id=task_id,
        session_id=session_id,
        message_length=len(user_message),
    )

    return StreamingResponse(
        stream_agent_events(
            agent_loop=_agent_loop,
            user_message=user_message,
            task_id=task_id,
            project_id=project_id,
            session_id=session_id,
            pending_interrupts=_pending_interrupts,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# A2A — HITL response
# ---------------------------------------------------------------------------


@app.post("/a2a/respond")
async def respond(request: Request) -> JSONResponse:
    """HITL response endpoint — resolves a paused ask_user_question interrupt.

    Body shape (apt-web chat UI 가 보내는 형태):

    - ``session_id`` (또는 ``id`` / ``task_id``) — :func:`stream_agent_events`
      가 ``input_required`` SSE 에 실어보낸 식별자
    - ``answer`` (또는 ``response``) — 사용자 선택지 id 혹은 자유 답변 텍스트

    Resolution flow:

    1. ``_pending_interrupts[session_id]`` 에서 ``Future`` 조회
    2. ``Future.set_result(answer)`` 로 깨움
    3. ``stream_agent_events`` generator 가 ``Command(resume=answer)`` 로
       LangGraph 그래프 재개 → 후속 SSE event 같은 stream 으로 emit
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    # apt-web chat UI 는 ``session_id`` 로 보낸다 (sse_emitter 가
    # ``input_required`` payload 의 ``session_id`` 필드로 emit). ``id`` /
    # ``task_id`` 는 backward-compat fallback.
    task_id = body.get("session_id") or body.get("id") or body.get("task_id", "")
    answer = body.get("answer") or body.get("response", "")

    if task_id and task_id in _pending_interrupts:
        entry = _pending_interrupts.pop(task_id)
        future = entry.get("future")
        if future and not future.done():
            future.set_result(answer)
            log.info("a2a.respond.resumed", task_id=task_id)
            return JSONResponse({"status": "resumed", "task_id": task_id})

    log.info("a2a.respond.no_pending", task_id=task_id, has_answer=bool(answer))
    return JSONResponse(
        {"status": "received", "task_id": task_id, "note": "no pending interrupt for this task"}
    )


# ---------------------------------------------------------------------------
# Process entry
# ---------------------------------------------------------------------------


def run() -> None:
    """Run the daemon under uvicorn. Honours ``PORT`` env (default 8080)."""
    import uvicorn

    uvicorn.run(
        "coding_agent.web.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
    )


if __name__ == "__main__":
    run()
