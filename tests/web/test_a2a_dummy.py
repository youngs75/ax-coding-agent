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
    """POST /a2a/stream returns SSE events."""
    resp = client.post("/a2a/stream", json={"message": "hello"})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    text = resp.text
    assert "event: task.start" in text
    assert "event: task.artifact" in text
    assert "event: task.end" in text


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
