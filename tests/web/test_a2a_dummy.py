"""Smoke tests for the bootstrap A2A daemon.

The daemon is a stub at this stage: only endpoint contracts are checked.
No LangGraph integration, no real LLM call, no SSE event streaming. The
tests below exercise every route registered in ``coding_agent.web.app``
and verify that responses are valid JSON envelopes with the expected
``status`` / ``name`` fields.

부트스트랩 단계 daemon 의 smoke 테스트. 아직 LangGraph 미통합·LLM 미호출·
SSE dummy 라서 endpoint contract(메서드/경로/JSON shape) 만 검증한다.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from coding_agent.web.app import app


@pytest.fixture
def client() -> TestClient:
    """Shared TestClient — host header defaults to ``testserver``.
    공유 TestClient — host 기본값 ``testserver``.
    """
    return TestClient(app)


def test_healthz_returns_ok(client: TestClient) -> None:
    """``GET /healthz`` returns ok with version string.
    /healthz 가 ok 와 버전 문자열을 반환하는지.
    """
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert isinstance(body.get("version"), str)


def test_well_known_agent_card(client: TestClient) -> None:
    """``GET /.well-known/agent.json`` returns a card with required fields.
    /.well-known/agent.json 카드의 필수 필드와 dynamic URL 검증.
    """
    resp = client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "ax-coding-agent"
    # endpoints dict must exist with the three documented entries.
    # endpoints dict 가 있고 3개 entry(tasksSend/tasksStream/respond) 모두 포함.
    endpoints = card.get("endpoints")
    assert isinstance(endpoints, dict)
    for key in ("tasksSend", "tasksStream", "respond"):
        assert key in endpoints
    # base URL must reflect the request host (TestClient default = testserver).
    # base URL 이 요청 host(=testserver)를 반영해야 한다.
    assert "testserver" in card["url"]
    assert "testserver" in endpoints["tasksSend"]
    # skills/capabilities sanity.
    # skills/capabilities 형태 확인.
    skills = card.get("skills") or []
    assert any(s.get("id") == "ax-coding-task" for s in skills)
    assert card["capabilities"]["streaming"] is True


def test_a2a_tasks_send_dummy(client: TestClient) -> None:
    """``POST /a2a/tasks/send`` returns the dummy ``received`` envelope.
    /a2a/tasks/send 가 dummy received envelope 를 반환.
    """
    resp = client.post("/a2a/tasks/send", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "received"
    assert "sync mode not implemented" in body.get("note", "")


@pytest.mark.parametrize("path", ["/a2a", "/a2a/jsonrpc", "/a2a/rest"])
def test_a2a_probe_fallbacks(client: TestClient, path: str) -> None:
    """Portal probe fallbacks delegate to the same dummy handler.
    포털 probe fallback 3개가 같은 dummy 핸들러로 위임되는지.
    """
    resp = client.post(path, json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "received"


def test_a2a_respond_dummy(client: TestClient) -> None:
    """``POST /a2a/respond`` returns the dummy HITL envelope.
    /a2a/respond 가 HITL dummy envelope 를 반환.
    """
    resp = client.post("/a2a/respond", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "received"
    assert "hitl not implemented" in body.get("note", "")


def test_all_responses_are_valid_json(client: TestClient) -> None:
    """Every JSON-returning route must produce parseable JSON.

    SSE stream is excluded — ``/a2a/stream`` returns ``text/event-stream``
    by design. The remaining routes are all JSON.

    JSON 반환 경로 전체가 유효 JSON 인지. SSE 인 /a2a/stream 은 제외 대상.
    """
    json_routes: list[tuple[str, str]] = [
        ("GET", "/healthz"),
        ("GET", "/.well-known/agent.json"),
        ("POST", "/a2a/tasks/send"),
        ("POST", "/a2a"),
        ("POST", "/a2a/jsonrpc"),
        ("POST", "/a2a/rest"),
        ("POST", "/a2a/respond"),
    ]
    for method, path in json_routes:
        resp = (
            client.get(path)
            if method == "GET"
            else client.post(path, json={})
        )
        assert resp.status_code == 200, f"{method} {path}"
        # ``json.loads`` raises on invalid JSON — that's the assertion.
        # 유효하지 않으면 json.loads 가 예외를 던져 테스트 실패.
        json.loads(resp.content)
