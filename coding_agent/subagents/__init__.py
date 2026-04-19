"""SubAgent 시스템 — minyoung-mah Orchestrator 기반.

Phase 6 refactor 후 구조:
  - ``roles`` : 6 coding roles (SubAgentRole protocol 구현)
  - ``orchestrator_factory.build_orchestrator`` : Orchestrator 조립
  - ``classifier`` : agent_type="auto" → role 분류
  - ``user_decisions`` : ask_user_question 답변 세션 로그
"""

from coding_agent.subagents.classifier import classify_task, resolve_role_name
from coding_agent.subagents.orchestrator_factory import build_orchestrator
from coding_agent.subagents.roles import (
    CodingAgentRole,
    ROLE_FACTORIES,
    coder_role,
    fixer_role,
    planner_role,
    researcher_role,
    reviewer_role,
    verifier_role,
)
from coding_agent.subagents.user_decisions import UserDecisionsLog

__all__ = [
    "CodingAgentRole",
    "ROLE_FACTORIES",
    "UserDecisionsLog",
    "build_orchestrator",
    "classify_task",
    "coder_role",
    "fixer_role",
    "planner_role",
    "researcher_role",
    "resolve_role_name",
    "reviewer_role",
    "verifier_role",
]
