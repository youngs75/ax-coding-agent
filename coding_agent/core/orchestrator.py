"""Orchestrator — 사용자 요청을 분석하고 직접 처리 vs SubAgent 위임을 결정한다."""

from __future__ import annotations

from typing import Any

import structlog
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from coding_agent.models import get_model

log = structlog.get_logger(__name__)

ORCHESTRATOR_SYSTEM_PROMPT = """당신은 AI Coding Agent의 오케스트레이터입니다.
사용자의 요청을 분석하여 다음 중 하나를 결정합니다:

1. "direct" — 간단한 질문, 설명, 짧은 코드 수정 등 직접 처리 가능한 작업
2. "delegate" — 복잡한 작업으로 SubAgent에 위임이 필요한 경우

위임이 필요한 경우 적절한 agent_type도 결정합니다:
- "planner": 아키텍처 설계, 요구사항 분석
- "coder": 코드 생성, 구현
- "reviewer": 코드 리뷰, 품질 검토
- "fixer": 버그 수정, 디버깅
- "researcher": 정보 검색, 분석

다음 JSON 형식으로만 응답하세요:
{"action": "direct"} 또는 {"action": "delegate", "agent_type": "coder", "task_summary": "요약"}
"""


def should_delegate(user_message: str) -> dict[str, Any]:
    """사용자 메시지를 분석하여 직접 처리 vs 위임을 결정한다.

    Returns:
        {"action": "direct"} 또는
        {"action": "delegate", "agent_type": str, "task_summary": str}
    """
    # 간단한 휴리스틱: 짧은 메시지는 직접 처리
    if len(user_message.strip()) < 20:
        return {"action": "direct"}

    # 명시적 위임 키워드
    delegate_keywords = [
        "만들어", "구현해", "작성해", "생성해", "수정해", "고쳐",
        "리팩토링", "테스트", "분석해", "설계해", "스캐폴딩",
        "create", "implement", "build", "fix", "refactor", "generate",
    ]
    msg_lower = user_message.lower()
    has_keyword = any(kw in msg_lower for kw in delegate_keywords)

    if not has_keyword:
        return {"action": "direct"}

    # LLM 분석으로 정확한 결정
    try:
        model = get_model("fast", temperature=0.0)
        response = model.invoke([
            SystemMessage(content=ORCHESTRATOR_SYSTEM_PROMPT),
            HumanMessage(content=user_message),
        ])
        content = response.content.strip()

        # JSON 파싱
        import json
        # 마크다운 코드 블록 제거
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        result = json.loads(content)
        if result.get("action") in ("direct", "delegate"):
            return result
    except Exception as e:
        log.warning("orchestrator.analysis_failed", error=str(e))

    # 폴백: 키워드가 있으면 coder에 위임
    return {
        "action": "delegate",
        "agent_type": "coder",
        "task_summary": user_message,
    }
