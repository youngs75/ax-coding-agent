"""Smoke tests for the A2A daemon.

Tests exercise every route registered in ``coding_agent.web.app`` with a
mocked AgentLoop so no LLM calls are made. The mock returns a minimal
final state that the endpoint handlers turn into A2A-compliant envelopes.

A2A daemon smoke 테스트. AgentLoop 를 mock 으로 교체하여 LLM 호출 없이
엔드포인트 contract(메서드/경로/JSON shape) 을 검증한다.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _make_client() -> TestClient:
    """Create a TestClient with a mocked AgentLoop."""
    mock_state = {
        "final_response": "mock response",
        "exit_reason": "completed",
        "iteration": 1,
        "messages": [],
    }

    mock_loop = MagicMock()
    mock_loop.run = AsyncMock(return_value=mock_state)
    mock_loop.close = MagicMock()

    with patch("coding_agent.web.app._agent_loop", mock_loop):
        # Must import app after patching so lifespan doesn't override
        from coding_agent.web.app import app

        client = TestClient(app, raise_server_exceptions=False)
        # Re-patch after lifespan may have reset it
        import coding_agent.web.app as app_module
        app_module._agent_loop = mock_loop
        yield client, mock_loop


@pytest.fixture
def client_and_loop():
    mock_state = {
        "final_response": "mock response",
        "exit_reason": "completed",
        "iteration": 1,
        "messages": [],
    }

    mock_loop = MagicMock()
    mock_loop.run = AsyncMock(return_value=mock_state)
    mock_loop.close = MagicMock()

    import coding_agent.web.app as app_module
    original = app_module._agent_loop
    app_module._agent_loop = mock_loop
    yield TestClient(app_module.app), mock_loop
    app_module._agent_loop = original


@pytest.fixture
def client(client_and_loop) -> TestClient:
    return client_and_loop[0]


@pytest.fixture
def mock_loop(client_and_loop):
    return client_and_loop[1]


def test_healthz_returns_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert isinstance(body.get("version"), str)


def test_well_known_agent_card(client: TestClient) -> None:
    resp = client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "ax-coding-agent"
    endpoints = card.get("endpoints")
    assert isinstance(endpoints, dict)
    for key in ("tasksSend", "tasksStream", "respond"):
        assert key in endpoints
    assert "testserver" in card["url"]
    assert "testserver" in endpoints["tasksSend"]
    skills = card.get("skills") or []
    assert any(s.get("id") == "ax-coding-task" for s in skills)
    assert card["capabilities"]["streaming"] is True


def test_a2a_tasks_send(client: TestClient, mock_loop) -> None:
    """POST /a2a/tasks/send invokes AgentLoop and returns A2A envelope."""
    resp = client.post("/a2a/tasks/send", json={"message": "hello"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"]["state"] == "completed"
    assert body["artifacts"][0]["parts"][0]["text"] == "mock response"
    mock_loop.run.assert_called_once()


def test_a2a_tasks_send_empty_message(client: TestClient) -> None:
    """Empty message returns 400."""
    resp = client.post("/a2a/tasks/send", json={"message": ""})
    assert resp.status_code == 400


@pytest.mark.parametrize("path", ["/a2a", "/a2a/jsonrpc", "/a2a/rest"])
def test_a2a_probe_fallbacks(client: TestClient, path: str) -> None:
    """Portal probe fallbacks delegate to the same handler."""
    resp = client.post(path, json={"message": "test"})
    assert resp.status_code == 200
    body = resp.json()
    assert "status" in body


def test_a2a_respond_no_pending(client: TestClient) -> None:
    """POST /a2a/respond with no pending interrupt returns received."""
    resp = client.post("/a2a/respond", json={"task_id": "fake", "answer": "yes"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "received"


def test_a2a_stream_returns_sse(client: TestClient, mock_loop) -> None:
    """POST /a2a/stream returns SSE with rich A2A event spec.

    SSE 스트림이 새 spec 이벤트를 emit 하는지 검증. mock_graph 가 빈
    astream_events + interrupts=None 을 반환하므로 기본 흐름만 통과.
    실제 LangGraph 통합은 EKS Pod E2E 검증이 담당.
    """
    # mock _graph — empty event stream + no interrupts
    async def fake_astream(*args, **kwargs):
        if False:  # never executes — turns this into an async generator
            yield  # pragma: no cover

    fake_state_snap = MagicMock(
        interrupts=None,
        values={"messages": [], "final_response": "mock final"},
    )
    mock_graph = MagicMock()
    mock_graph.astream_events = fake_astream
    mock_graph.aget_state = AsyncMock(return_value=fake_state_snap)
    mock_loop._graph = mock_graph

    resp = client.post("/a2a/stream", json={"message": "hello"})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    text = resp.text

    # Rich A2A spec — apt-web chat UI 가 시각화하는 이벤트 이름
    assert "event: orchestrator.run.start" in text
    assert "event: orchestrator.run.end" in text
    # session_id 가 SSE payload 에 들어있어야 (apt-web 의 hitlModal.session_id 기반)
    assert "\"session_id\":" in text


def test_a2a_stream_emits_input_required_on_interrupt(client: TestClient, mock_loop) -> None:
    """When AgentLoop hits an ask_user_question interrupt, the stream emits
    ``input_required`` and registers a pending Future for ``/a2a/respond``.

    AgentLoop 이 ask_user_question interrupt 에 도달하면 SSE 가
    ``input_required`` event 를 emit 하고 ``_pending_interrupts`` 에 Future 를
    등록한다 (HITL 라운드트립의 절반 검증 — 답변 라운드트립은 별도 단위 테스트).
    """
    pytest.importorskip("langgraph")
    import asyncio

    async def fake_astream(*args, **kwargs):
        if False:
            yield  # pragma: no cover

    interrupt_payload = {
        "kind": "ask_user_question",
        "question": "Vue 또는 React?",
        "choices": [{"id": "vue", "label": "Vue"}, {"id": "react", "label": "React"}],
        "allow_free_text": False,
    }
    interrupt_marker = MagicMock(value=interrupt_payload)
    fake_state_with_interrupt = MagicMock(
        interrupts=[interrupt_marker],
        values={"messages": [], "final_response": ""},
    )
    mock_graph = MagicMock()
    mock_graph.astream_events = fake_astream
    mock_graph.aget_state = AsyncMock(return_value=fake_state_with_interrupt)
    mock_loop._graph = mock_graph

    # 답변이 도착하지 않으면 stream_agent_events 가 5분 대기 — 테스트 빠르게
    # 끝내기 위해 _HITL_TIMEOUT_S 를 짧게 monkey-patch.
    import coding_agent.web.sse_emitter as emitter_mod
    original_timeout = emitter_mod._HITL_TIMEOUT_S
    emitter_mod._HITL_TIMEOUT_S = 0.5  # 0.5s timeout for test

    try:
        resp = client.post("/a2a/stream", json={"message": "hello"})
        assert resp.status_code == 200
        text = resp.text
        # input_required 가 SSE 로 발행됨
        assert "event: input_required" in text
        assert "Vue 또는 React?" in text
        # timeout 후 run.end 가 success=False 로
        assert "event: orchestrator.run.end" in text
    finally:
        emitter_mod._HITL_TIMEOUT_S = original_timeout


def test_a2a_respond_resumes_pending_interrupt() -> None:
    """``/a2a/respond`` resolves the registered Future so the SSE stream
    can resume.

    ``/a2a/respond`` 가 등록된 Future 를 resolve 해서 SSE stream 이 재개되는지.
    """
    import asyncio
    import coding_agent.web.app as app_module

    loop_ = asyncio.new_event_loop()
    try:
        future = loop_.create_future()
        app_module._pending_interrupts["session-xyz"] = {
            "future": future,
            "thread_id": "a2a-session-xyz",
        }

        client = TestClient(app_module.app)
        resp = client.post(
            "/a2a/respond",
            json={"session_id": "session-xyz", "answer": "vue"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "resumed"

        # Future 가 resolve 됐는지 (loop_.run_until_complete 로 결과 확인)
        assert future.done()
        assert future.result() == "vue"
        # _pending_interrupts 에서 제거됐는지
        assert "session-xyz" not in app_module._pending_interrupts
    finally:
        loop_.close()
        app_module._pending_interrupts.pop("session-xyz", None)


def test_all_json_routes_return_valid_json(client: TestClient) -> None:
    """Every JSON-returning route must produce parseable JSON.
    SSE stream excluded.
    """
    json_routes: list[tuple[str, str, dict]] = [
        ("GET", "/healthz", {}),
        ("GET", "/.well-known/agent.json", {}),
        ("POST", "/a2a/tasks/send", {"message": "test"}),
        ("POST", "/a2a", {"message": "test"}),
        ("POST", "/a2a/jsonrpc", {"message": "test"}),
        ("POST", "/a2a/rest", {"message": "test"}),
        ("POST", "/a2a/respond", {}),
    ]
    for method, path, body in json_routes:
        resp = (
            client.get(path)
            if method == "GET"
            else client.post(path, json=body)
        )
        assert resp.status_code in (200, 400), f"{method} {path} → {resp.status_code}"
        json.loads(resp.content)
