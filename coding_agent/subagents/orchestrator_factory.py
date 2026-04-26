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
    Orchestrator,
    RoleRegistry,
    TieredModelRouter,
    ToolRegistry,
    default_resilience,
)
from minyoung_mah.hitl.channels import QueueHITLChannel

from coding_agent.config import get_config
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

# critic_role 은 ``coding_agent.subagents.roles`` 의 ``CodingAgentRole`` 등을
# 재사용하므로 module-level import 시 ``coding_agent.subagents.__init__``
# 패키지 로드와 순환 충돌. ``build_orchestrator`` 안에서 lazy import 한다.
from coding_agent.tools.adapters import (
    FILE_ADAPTERS,
    SHELL_ADAPTERS,
    build_todo_adapters,
)
from coding_agent.tools.ask_adapter import ask_user_question_adapter

if TYPE_CHECKING:
    from collections.abc import Callable

    from minyoung_mah.core.protocols import ToolAdapter

    from coding_agent.subagents.user_decisions import UserDecisionsLog
    from coding_agent.tools.todo_tool import TodoItem, TodoStore


def build_orchestrator(
    memory_store: MemoryStore,
    user_decisions: "UserDecisionsLog",
    todo_store: "TodoStore | None" = None,
    todo_change_callback: "Callable[[list[TodoItem]], None] | None" = None,
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
    if todo_store is not None:
        for adapter in build_todo_adapters(todo_store, on_change=todo_change_callback):
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
    # v22.2 — ledger SubAgent 폐기. write_todos 는 planner 가 직접 호출.
    # update_todo 는 task_tool 의 _on_end auto-advance + (필요 시) orchestrator
    # top-level 도구로 처리.
    # critic role — sufficiency loop 가 켜진 경우에만 등록. 끄면 invoke_role
    # 호출이 일어나지 않으므로 이 등록을 가드해 토큰 비용·로그 노이즈를
    # 줄인다. apt-legal 패턴.
    cfg = get_config()
    if cfg.sufficiency_enabled:
        from coding_agent.sufficiency.critic_role import critic_role
        role_registry.register(critic_role(user_decisions=user_decisions))

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

    # ── HITL ──
    # ax 의 ``ask_user_question`` 경로는 LangGraph interrupt 를 통해 처리
    # 되므로 여기 HITL 채널은 ``ask`` 가 아닌 ``notify`` 만 쓴다.
    # QueueHITLChannel 의 ``notifications`` 큐로 sufficiency critic_escalate
    # 이벤트를 흘려보내고, CLI 가 polling 해서 Rich panel 로 표시한다.
    # plan §결정 3 — interrupt 경로는 그대로 LangGraph 가 담당.
    hitl_channel = QueueHITLChannel()

    return Orchestrator(
        role_registry=role_registry,
        tool_registry=tool_registry,
        model_router=model_router,
        memory=memory_store,
        hitl=hitl_channel,
        observer=build_default_observer(),
        resilience=resilience,
    )


__all__ = ["build_orchestrator"]
