"""AgentState — LangGraph 그래프 전체에서 공유하는 상태 정의."""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict, total=False):
    """메인 에이전트 루프의 상태.

    LangGraph StateGraph가 이 상태를 기반으로 노드 간 데이터를 전달한다.
    """

    # ── 메시지 히스토리 (LangGraph add_messages 리듀서) ──
    messages: Annotated[list[AnyMessage], add_messages]

    # ── 루프 제어 ──
    iteration: int  # 현재 반복 횟수
    max_iterations: int  # 최대 반복 한도
    current_tier: str  # 현재 사용 중인 모델 티어

    # ── 종료 상태 ──
    exit_reason: str  # 종료 사유 (completed, safe_stop, error, max_iterations)
    final_response: str  # 최종 응답

    # ── 에러 / 복원력 ──
    error_info: dict[str, Any]  # ErrorHandler 출력
    resume_metadata: dict[str, Any]  # 재개용 메타데이터
    stall_count: int  # 연속 무진전 횟수

    # ── 메모리 ──
    memory_context: str  # 주입된 메모리 블록 (시스템 프롬프트에 추가)
    project_id: str  # 현재 프로젝트 식별자

    # ── SubAgent ──
    subagent_results: dict[str, Any]  # SubAgent 결과 저장소

    # ── 작업 디렉토리 ──
    working_directory: str  # 현재 작업 디렉토리
