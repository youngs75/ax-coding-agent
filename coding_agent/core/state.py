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

    # ── Pending-todo 종료 방어 ──
    # orchestrator 가 tool_calls=None 으로 종료 시도했는데 ledger 에 pending
    # 항목이 남아있으면 harness 가 nudge 메시지를 주입해 재시도시킨다.
    # ``pending_nudges`` 는 "진전 없는 연속 silent-terminate 횟수" — 직전
    # nudge 시점 대비 unfinished 가 줄어들면 0 으로 리셋된다. qwen3-max 처럼
    # 매 배치 완료 후 자연어 보고를 반복하는 모델도 진전이 있는 한 계속
    # 찌를 수 있게 함. _MAX_STUCK_NUDGES 도달 시에만 재시도를 포기한다.
    pending_nudges: int
    # 직전 nudge 시점의 pending+in_progress 합. progress 판정 baseline.
    last_nudge_unfinished: int

    # ── 분해 확인 (planner→ledger 등록 후 HITL 게이트) ──
    # ledger 가 초기 등록을 마친 뒤, coder/verifier/fixer/reviewer 로 위임하기
    # 전에 사용자에게 granularity 를 확인받아야 한다. False 이면 게이트가
    # 한 번 차단하고 안내 메시지를 주입한 뒤 True 로 전환 — 프롬프트가
    # 이어서 ask_user_question 을 호출하도록 유도한다. 모델 순종도에 완전
    # 의존하지 않는 harness 안전망.
    decomposition_confirmed: bool

    # ── 작업 디렉토리 ──
    working_directory: str  # 현재 작업 디렉토리

    # ── Sufficiency loop (apt-legal 패턴 이식) ──
    # 모든 task 가 COMPLETED 인 시점에 한 번 더 "사용자 요청 충족도"를
    # rule_gate + critic LLM 으로 평가. config.sufficiency_enabled=True 일
    # 때만 그래프가 sufficiency_gate 노드로 진입한다. retry/replan 으로
    # agent 노드 복귀 시 sufficiency_pending_feedback 이 다음 진입 시점에
    # HumanMessage 로 변환된다. 모든 필드 reducer 없음 (덮어쓰기).
    sufficiency_iterations: int  # 누적 outer-loop 반복 횟수
    last_critic_verdict: dict[str, Any]  # CriticVerdict 직렬화
    needs_human_review: bool
    sufficiency_history: list[dict[str, Any]]  # SufficiencyHistoryEntry 직렬화
    sufficiency_pending_feedback: str  # 다음 agent 진입 시 HumanMessage 로 주입할 텍스트
