"""Task → role 분류기.

기존 ``SubAgentFactory._analyze_task`` (keyword fast-path + fast-LLM fallback)
를 독립 모듈로 추출. task_tool 이 ``agent_type="auto"`` 로 들어온 description
을 role name 으로 분류할 때 사용.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from coding_agent.models import get_model

log = structlog.get_logger(__name__)


_ROLE_KEYWORDS: dict[str, list[str]] = {
    "planner": [
        "설계", "계획", "분석", "아키텍처", "PRD", "SPEC", "요구사항",
        "plan", "design", "architect", "analyze", "requirement",
    ],
    "coder": [
        "구현", "작성", "생성", "코드", "코딩", "만들", "설치",
        "implement", "create", "write", "code", "build", "install", "setup",
    ],
    "reviewer": [
        "리뷰", "검토", "review", "audit",
    ],
    "fixer": [
        "수정", "fix", "bug", "오류", "에러", "디버그", "debug", "repair", "실패",
    ],
    "researcher": [
        "조사", "탐색", "찾", "search", "research", "find", "explore",
    ],
    "verifier": [
        "테스트", "검증", "확인", "빌드", "실행",
        "test", "verify", "check", "build", "run test", "validate",
    ],
}

_KNOWN_ROLES = set(_ROLE_KEYWORDS.keys())

_CLASSIFY_PROMPT = """\
You are a task classifier. Given a task description, determine the single best
agent role from the following list:

- planner: for tasks that require architecture planning, design decisions, or creating step-by-step plans
- coder: for tasks that require writing, generating, or implementing code
- reviewer: for tasks that require reviewing, auditing, or critiquing existing code
- fixer: for tasks that require debugging, fixing bugs, or resolving errors
- researcher: for tasks that require searching, reading, or gathering information
- verifier: for tasks that require running tests, checking builds, or verifying implementations

Respond with ONLY the role name (one word, lowercase). No explanation.

Task: {task_description}
"""


def classify_task(task_description: str) -> str:
    """Classify *task_description* into one of the 6 known role names.

    Fast keyword scoring first (0 API calls); fast-tier LLM fallback only
    when no keyword hits. Always returns a valid role from ``_KNOWN_ROLES``.
    """
    t0 = time.monotonic()
    desc_lower = task_description.lower()

    # Fast path: keyword matching (0ms, no API call)
    scores: dict[str, int] = {}
    for role, keywords in _ROLE_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in desc_lower)
        if score > 0:
            scores[role] = score

    if scores:
        role = max(scores, key=scores.get)  # type: ignore[arg-type]
        log.info(
            "timing.classify_fast",
            task=task_description[:80],
            role=role,
            score=scores[role],
            elapsed_s=round(time.monotonic() - t0, 4),
        )
        return role

    # Slow path: LLM classification when keywords don't match.
    try:
        fast_llm = get_model("fast", temperature=0.0)
        prompt = _CLASSIFY_PROMPT.format(task_description=task_description)
        response = fast_llm.invoke(prompt)
        content = getattr(response, "content", "") or ""
        role = content.strip().lower().split()[0] if content else "coder"

        if role not in _KNOWN_ROLES:
            log.warning(
                "classifier.fallback_unknown_role",
                raw_response=role,
                fallback="coder",
            )
            role = "coder"

        log.info(
            "timing.classify_llm",
            task=task_description[:80],
            role=role,
            elapsed_s=round(time.monotonic() - t0, 3),
        )
        return role
    except Exception as exc:
        log.error("classifier.error", error=str(exc), fallback="coder")
        return "coder"


def resolve_role_name(agent_type: str, task_description: str) -> str:
    """Map ``agent_type`` (caller hint) + description to a concrete role.

    - ``"auto"`` → :func:`classify_task` (keyword or LLM).
    - Known role name → returned as-is.
    - Unknown → log warning, fall back to ``"coder"``.
    """
    if agent_type == "auto":
        return classify_task(task_description)
    if agent_type in _KNOWN_ROLES:
        return agent_type
    log.warning("classifier.unknown_role", requested=agent_type, fallback="coder")
    return "coder"


__all__ = ["classify_task", "resolve_role_name"]
