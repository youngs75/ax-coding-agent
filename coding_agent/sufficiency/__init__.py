"""Sufficiency loop — apt-legal 패턴 이식 (코딩 도메인 적응).

LangGraph 그래프의 종료 직전 (모든 task COMPLETED 시점) 에 한 번 더
"사용자 요청이 충분히 충족됐는가" 를 평가하는 outer loop.

Layers:
- ``schemas`` : data types (CriticVerdict / CodeQualityGateResult / ...)
- ``signals`` : agent state / messages / todo_store 에서 deterministic 신호 추출
- ``rules``   : 신호 → HIGH/MEDIUM/LOW + LOW 휴리스틱 verdict 생성
- ``critic_role`` : LLM critic SubAgentRole 정의 (read-only, reasoning tier)
- ``critic`` : invoke_role("critic", ...) 호출 + JSON 응답 정규화
- ``loop``   : iteration / cycle / feedback 누적 + observer.emit + hitl.notify

기능 플래그 ``Config.sufficiency_enabled`` 로 점진 도입.
비활성 시 ``coding_agent/core/loop.py`` 의 route_after_agent 가 분기를
타지 않으므로 본 패키지 코드는 호출되지 않는다.
"""

from coding_agent.sufficiency.schemas import (
    CodeQualityGateResult,
    CriticVerdict,
    SufficiencyHistoryEntry,
    SufficiencyLoopResult,
)

__all__ = [
    "CodeQualityGateResult",
    "CriticVerdict",
    "SufficiencyHistoryEntry",
    "SufficiencyLoopResult",
]
