"""minyoung_mah.Orchestrator 조립 — ax SubAgent Orchestrator 빌더.

ax 의 AgentLoop 가 생성 시점에 호출. 6개 role + file/shell adapters + HITL/
memory/observer/resilience 를 구성해 Orchestrator 인스턴스 하나를 돌려준다.

Phase 6 의 task_tool 이 이 Orchestrator 의 ``invoke_role`` 을 호출해 실제
SubAgent 실행을 수행한다 (기존 SubAgentManager.execute 경로 대체).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minyoung_mah import (
    MemoryStore,
    NullHITLChannel,
    Orchestrator,
    RoleRegistry,
    TieredModelRouter,
    ToolRegistry,
    default_resilience,
)

from coding_agent.observability import build_default_observer

from coding_agent.models import get_model
from coding_agent.subagents.roles import (
    coder_role,
    fixer_role,
    planner_role,
    researcher_role,
    reviewer_role,
    verifier_role,
)
from coding_agent.tools.adapters import FILE_ADAPTERS, SHELL_ADAPTERS
from coding_agent.tools.ask_adapter import ask_user_question_adapter

if TYPE_CHECKING:
    from minyoung_mah.core.protocols import ToolAdapter

    from coding_agent.subagents.user_decisions import UserDecisionsLog


def build_orchestrator(
    memory_store: MemoryStore,
    user_decisions: "UserDecisionsLog",
    extra_adapters: "list[ToolAdapter] | None" = None,
    role_timeouts: dict[str, float] | None = None,
) -> Orchestrator:
    """Assemble the SubAgent Orchestrator with ax's 6 coding roles.

    Parameters
    ----------
    memory_store:
        ``SqliteMemoryStore`` instance owned by :class:`AgentLoop` — reused
        here so roles that later want to write to memory share the same
        backing file.
    user_decisions:
        Session-scoped accumulator of ``ask_user_question`` answers. Each
        role's ``build_user_message`` prepends its ``header()`` block.
    extra_adapters:
        Additional ``ToolAdapter`` instances registered alongside the
        file + shell defaults (e.g. an ``ask_user_question`` adapter once
        Phase 6 rewires the HITL path).
    role_timeouts:
        Per-role watchdog timeouts in seconds. Defaults to sane values
        matching the pre-refactor Manager (planner 300s, coder 240s,
        verifier 90s, fixer 90s, reviewer 180s, researcher 120s).
    """
    tool_registry = ToolRegistry()
    for adapter in (*FILE_ADAPTERS, *SHELL_ADAPTERS, ask_user_question_adapter):
        tool_registry.register(adapter)
    for adapter in extra_adapters or []:
        tool_registry.register(adapter)

    role_registry = RoleRegistry()
    role_registry.register(planner_role(user_decisions=user_decisions))
    role_registry.register(coder_role(user_decisions=user_decisions))
    role_registry.register(reviewer_role(user_decisions=user_decisions))
    role_registry.register(fixer_role(user_decisions=user_decisions))
    role_registry.register(researcher_role(user_decisions=user_decisions))
    role_registry.register(verifier_role(user_decisions=user_decisions))

    # ── Model router: one shared model per tier ──
    # Building the ChatOpenAI instances here keeps the Orchestrator wiring
    # standalone. TieredModelRouter caches the instance for (tier, role);
    # role_overrides are not needed for ax at present.
    tier_models = {
        "reasoning": get_model("reasoning", temperature=0.0),
        "strong": get_model("strong", temperature=0.0),
        "default": get_model("default", temperature=0.0),
        "fast": get_model("fast", temperature=0.0),
    }
    model_router = TieredModelRouter(tiers=tier_models)

    # ── Resilience ──
    # Each role is max_iterations=100 bounded by construction, so the library
    # progress_guard stays disabled at the SubAgent level. The top-level ax
    # guard inside AgentLoop still catches verifier↔fixer TASK-NN repeats at
    # the orchestrator-over-subagents layer (plan §결정 4).
    timeouts = role_timeouts or {
        "planner": 300.0,
        "coder": 240.0,
        "reviewer": 180.0,
        "fixer": 90.0,
        "researcher": 120.0,
        "verifier": 90.0,
    }
    resilience = default_resilience(role_timeouts=timeouts)

    return Orchestrator(
        role_registry=role_registry,
        tool_registry=tool_registry,
        model_router=model_router,
        memory=memory_store,
        hitl=NullHITLChannel(),  # plan §결정 3 — interrupt path stays on LangGraph
        observer=build_default_observer(),
        resilience=resilience,
    )


__all__ = ["build_orchestrator"]
