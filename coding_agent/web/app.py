"""FastAPI daemon entry for ax-coding-agent.

Bootstrap stub: every endpoint contract from the apt-legal-agent A2A
pattern is registered, but the bodies are dummies. Real LangGraph wiring,
SSE event streaming, and HITL response handling land in a follow-up
commit. The goal here is "process is up, healthz/agent_card respond,
portal probes don't 404".

부트스트랩 단계 — apt-legal-agent A2A 패턴의 endpoint 8 종을 모두 등록하되
본체는 dummy 응답이다. 실제 LangGraph 통합·SSE·HITL 은 다음 commit. 지금
목표는 "프로세스가 떠 있고 healthz/agent_card 가 응답하며 포털 probe 가
404 를 받지 않는 것".

Routes:

- ``GET  /healthz``                  — liveness probe.
- ``GET  /.well-known/agent.json``   — A2A agent card (dynamic host).
- ``POST /a2a/tasks/send``           — sync JSON-RPC task (dummy).
- ``POST /a2a/stream``               — SSE streaming task (dummy single event).
- ``POST /a2a/respond``              — HITL response submission (dummy).
- ``POST /a2a``                      — portal probe fallback → tasks/send.
- ``POST /a2a/jsonrpc``              — portal probe fallback → tasks/send.
- ``POST /a2a/rest``                 — portal probe fallback → tasks/send.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .agent_card import _resolve_version, build_agent_card

__version__ = _resolve_version()

app = FastAPI(title="ax-coding-agent", version=__version__)


# ---------------------------------------------------------------------------
# Liveness / agent card
# 라이브니스 / 에이전트 카드
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """K8s liveness probe — always ok in bootstrap mode.
    K8s liveness probe — 부트스트랩 단계에서는 항상 ok 를 돌려준다.
    """
    return {"status": "ok", "version": __version__}


@app.get("/.well-known/agent.json")
async def well_known_agent(request: Request) -> dict[str, Any]:
    """Dynamic A2A agent card from the incoming request.
    들어온 요청을 기반으로 동적 A2A agent card 를 생성한다.
    """
    return build_agent_card(request)


# ---------------------------------------------------------------------------
# A2A — synchronous tasks/send (+ portal probe fallbacks)
# A2A — 동기 tasks/send (+ 포털 probe 호환 alias)
# ---------------------------------------------------------------------------


async def _handle_send(request: Request) -> JSONResponse:
    """Dummy synchronous task handler.

    Reads the request body to verify JSON parsing works end-to-end, then
    returns a stub envelope. Real orchestrator dispatch arrives later.

    동기 task dummy 핸들러 — 본문 JSON 파싱이 동작하는지만 확인하고 스텁
    응답을 돌려준다. 실제 orchestrator 위임은 추후 단계.
    """
    try:
        await request.json()
    except Exception:
        # Empty body / invalid JSON is fine in bootstrap mode.
        # 부트스트랩에선 빈 본문/잘못된 JSON 모두 허용.
        pass
    return JSONResponse(
        {"status": "received", "note": "sync mode not implemented yet"}
    )


# Portal Playground probes /a2a, /a2a/jsonrpc, /a2a/rest in order before
# falling through to the canonical /a2a/tasks/send. Register all four.
# 포털 플레이그라운드는 /a2a → /a2a/jsonrpc → /a2a/rest → /a2a/tasks/send
# 순으로 probe 한다. 네 경로 모두 같은 핸들러로 등록.
app.post("/a2a/tasks/send")(_handle_send)
app.post("/a2a")(_handle_send)
app.post("/a2a/jsonrpc")(_handle_send)
app.post("/a2a/rest")(_handle_send)


# ---------------------------------------------------------------------------
# A2A — streaming task (SSE dummy)
# A2A — 스트리밍 task (SSE dummy)
# ---------------------------------------------------------------------------


async def _dummy_stream():
    """Yield a single SSE event signaling the stub start.

    Real implementation will wire ``Orchestrator`` events to SSE frames
    via ``QueueHITLChannel``-style notifications. For now we emit one
    event so clients can verify the SSE contract end-to-end.

    한 번 발행되는 dummy SSE event. 실제 구현은 Orchestrator notifications
    를 SSE frame 으로 변환할 예정. 지금은 클라이언트가 SSE contract 자체를
    검증할 수 있도록 단일 이벤트만 보낸다.
    """
    yield (
        b"event: orchestrator.run.start\n"
        b"data: {\"note\":\"streaming not implemented yet\"}\n\n"
    )


@app.post("/a2a/stream")
async def tasks_stream(request: Request) -> StreamingResponse:
    """Dummy SSE stream — emits one ``orchestrator.run.start`` and ends.
    dummy SSE 스트림 — orchestrator.run.start 한 번 발행 후 종료.
    """
    try:
        await request.json()
    except Exception:
        pass
    return StreamingResponse(
        _dummy_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# A2A — HITL response (dummy)
# A2A — HITL 응답 (dummy)
# ---------------------------------------------------------------------------


@app.post("/a2a/respond")
async def respond(request: Request) -> JSONResponse:
    """Dummy HITL response endpoint — answers from a paused interrupt.
    dummy HITL respond endpoint — 일시정지된 interrupt 의 사용자 응답 수신.
    """
    try:
        await request.json()
    except Exception:
        pass
    return JSONResponse(
        {"status": "received", "note": "hitl not implemented yet"}
    )


# ---------------------------------------------------------------------------
# Process entry — `ax-server` script binds here.
# 프로세스 진입점 — pyproject 의 `ax-server` 스크립트가 이 함수로 매핑.
# ---------------------------------------------------------------------------


def run() -> None:
    """Run the daemon under uvicorn. Honours ``PORT`` env (default 8080).
    uvicorn 으로 daemon 을 띄운다. ``PORT`` env 가 있으면 그 값(기본 8080).
    """
    import uvicorn

    uvicorn.run(
        "coding_agent.web.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
    )


if __name__ == "__main__":
    run()
