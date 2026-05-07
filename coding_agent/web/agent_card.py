"""Agent card builder — served at ``/.well-known/agent.json``.

Mirrors the apt-legal-agent A2A pattern: the card is built dynamically
from the incoming HTTP request so advertised endpoint URLs always match
the actual host (works behind reverse proxies, on localhost, in EKS).

apt-legal-agent 의 A2A 패턴을 따른다 — agent card 는 들어온 요청에서
host/scheme 을 읽어 동적으로 만든다(reverse proxy/EKS/localhost 무관 동작).
"""

from __future__ import annotations

from importlib import metadata
from typing import Any

from fastapi import Request


def _resolve_version() -> str:
    """Resolve the package version from pyproject metadata.
    pyproject 메타데이터에서 패키지 버전 조회 (실패 시 0.0.0 fallback).
    """
    try:
        return metadata.version("ax-coding-agent")
    except metadata.PackageNotFoundError:
        # editable/dev mode without install — fall back to a sentinel.
        # editable 설치 안 된 dev 환경 fallback.
        return "0.0.0"


def _resolve_base_url(request: Request) -> str:
    """Build the canonical base URL from the incoming request.
    들어온 요청 헤더에서 scheme/host 를 읽어 base URL 을 구성한다.
    """
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    return f"{scheme}://{host}"


def build_agent_card(request: Request) -> dict[str, Any]:
    """Construct the A2A agent card dict for ``/.well-known/agent.json``.
    A2A agent card dict 를 생성한다 (FastAPI 핸들러가 그대로 직렬화).
    """
    base_url = _resolve_base_url(request)
    version = _resolve_version()

    return {
        "name": "ax-coding-agent",
        "version": version,
        "description": (
            "코드 작성·검증 멀티 에이전트 (LangGraph + 6 SubAgent + auto-verify chain)"
        ),
        "url": base_url,
        "protocol": "a2a",
        "protocolVersion": "0.1",
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "authentication": {"schemes": ["none"]},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [
            {
                "id": "ax-coding-task",
                "name": "코드 작성 임무",
                "description": (
                    "사용자 요구를 받아 LangGraph 기반 6-role 멀티 에이전트가 "
                    "계획 → 구현 → 검토 → 자동검증을 거쳐 코드 산출물을 만든다."
                ),
                "tags": ["coding", "langgraph", "multi-agent"],
                "examples": [
                    "React 데모 앱 만들어줘",
                    "Python FastAPI 서버 구현해줘",
                ],
            }
        ],
        "endpoints": {
            "tasksSend": f"{base_url}/a2a/tasks/send",
            "tasksStream": f"{base_url}/a2a/stream",
            "respond": f"{base_url}/a2a/respond",
            # Workspace 산출물 — zip 전체 + path 단위 단일 파일.
            "artifactsBundle": f"{base_url}/artifacts/__bundle.zip",
            "artifactsFile": f"{base_url}/artifacts/{{path}}",
            # Workspace 초기화 — 사용자 생성 파일 전체 삭제.
            "workspaceReset": f"{base_url}/workspace/reset",
        },
    }


__all__ = ["build_agent_card"]
