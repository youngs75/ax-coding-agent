"""메인 Agentic Loop — LangGraph StateGraph 기반 에이전트 실행 루프.

전체 흐름:
    START → inject_memory → agent → route_after_agent
        ├→ tools → extract_memory → check_progress → agent (루프)
        ├→ extract_memory → END (완료)
        └→ handle_error → route_after_error
            ├→ agent (재시도/폴백)
            └→ safe_stop → END (중단)

DeepAgents의 middleware 패턴 + Claude Code의 compaction + Codex의 상태 관리를 결합.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from pathlib import Path
from typing import Any

import structlog
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

from coding_agent.config import get_config
from coding_agent.core.state import AgentState
from coding_agent.core.tool_adapter import (
    bind_tools_adaptive,
    build_tool_prompt,
    convert_text_response_to_tool_calls,
    invoke_with_tool_fallback,
)
from coding_agent.core.tool_call_utils import prepare_messages_for_llm
from coding_agent.memory import MemoryExtractor, MemoryMiddleware, MemoryStore
from coding_agent.models import get_model, get_fallback_model, get_model_name, TierName
from minyoung_mah.context import ContextManager, default_policy
from minyoung_mah.resilience.progress_guard import GuardVerdict, ProgressGuard
from coding_agent.resilience_compat import (
    ErrorHandler,
    SafeStop,
    Watchdog,
)
from coding_agent.subagents.orchestrator_factory import build_orchestrator
from coding_agent.subagents.user_decisions import UserDecisionsLog
from coding_agent.sufficiency import critic as _critic_mod
from coding_agent.sufficiency import loop as _suff_loop
from coding_agent.sufficiency import rules as _suff_rules
from coding_agent.sufficiency import signals as _suff_signals
from coding_agent.sufficiency.schemas import CriticVerdict as _CriticVerdict
from coding_agent.tools.file_ops import FILE_TOOLS
from coding_agent.tools.shell import SHELL_TOOLS
from coding_agent.tools.task_tool import build_task_tool
from coding_agent.tools.todo_tool import TodoStore

# Async callback type for satisfying ask_user_question interrupts.
# Receives the interrupt payload (dict) and returns the user's answer.
from typing import Awaitable, Callable

AskUserCallback = Callable[[Any], Awaitable[Any]]

log = structlog.get_logger(__name__)

# Secondary-key extractor for ProgressGuard. When the top-level orchestrator
# delegates a task with a description starting "TASK-NN:", repeated delegations
# to the same id (verifier↔fixer cycles with slightly different prose) collapse
# onto the same key, so the library guard catches them.
_TASK_ID_PATTERN = re.compile(r"\bTASK-\d{2,}\b", re.IGNORECASE)


def _task_id_extractor(tool_name: str, tool_args: dict) -> str | None:
    if tool_name != "task":
        return None
    desc = tool_args.get("description") if isinstance(tool_args, dict) else None
    if not isinstance(desc, str):
        return None
    m = _TASK_ID_PATTERN.search(desc)
    return m.group(0).upper() if m else None

# Roles that should be gated by decomposition confirmation. Delegating to
# these before the user has approved the task breakdown is the anti-pattern
# v2 E2E (2026-04-22) exposed — planner returned 8 coarse tasks, orchestrator
# went straight to coder without user review, and coarse tasks led coder to
# decide completion granularity unilaterally. Ledger/planner/researcher are
# NOT gated — planner may need re-delegation, and researcher/read-only
# exploration is safe. (v22.2: ledger SubAgent 폐기 — planner 가 write_todos
# 직접 호출하므로 ledger gating 항목 자체가 사라짐.)
_GATED_ROLES = frozenset({"coder", "verifier", "fixer", "reviewer"})


def _requires_decomposition_gate(
    last_message: Any,
    todo_counts: dict[str, int],
    confirmed: bool,
) -> tuple[bool, str | None]:
    """Decide whether to block before a gated task delegation.

    Returns ``(gate_needed, blocked_tool_call_id)``. The id points at the
    first offending ``task`` tool_call so the gate node can respond with a
    ``ToolMessage`` — otherwise LangChain would raise about an orphan call.

    Pure — callers pass in the last AIMessage (or whatever is at the tail
    of the message list), the current ``TodoStore.counts()`` snapshot, and
    the ``decomposition_confirmed`` flag.
    """
    if confirmed:
        return (False, None)
    total = sum(todo_counts.values())
    if total == 0:
        return (False, None)
    tool_calls = getattr(last_message, "tool_calls", None) if last_message is not None else None
    if not tool_calls:
        return (False, None)
    for tc in tool_calls:
        if tc.get("name") != "task":
            continue
        args = tc.get("args") or {}
        agent_type = (args.get("agent_type") or "").strip().lower()
        if agent_type in _GATED_ROLES:
            return (True, tc.get("id"))
    return (False, None)


def _build_decomposition_interrupt_payload(
    counts: dict[str, int],
    preview: list[str],
) -> dict[str, Any]:
    """Build the ``ask_user_question`` payload the harness raises via ``interrupt()``.

    Mirrors the format produced by the ``ask_user_question`` tool so the
    CLI's ``question_renderer`` handles both code paths uniformly. Threshold
    advisory: total > 15 → suggest consolidating, total < 4 → suggest finer
    split, otherwise neutral. Text goes directly to the user (not the model).
    """
    total = sum(counts.values())
    shown = preview[:5]
    preview_block = "\n".join(f"  - {line}" for line in shown)
    if total > len(shown):
        preview_block += f"\n  ... 외 {total - len(shown)}개"

    if total > 15:
        advisory = f" (총 {total}개 — 일반 권고: 5~15개. 세분화가 과한 것 같습니다.)"
    elif total < 4:
        advisory = f" (총 {total}개 — 일반 권고: 5~15개. 통합이 과한 것 같습니다.)"
    else:
        advisory = f" (총 {total}개)"

    question_text = (
        f"task 분해 결과 미리보기{advisory}\n\n"
        f"{preview_block}\n\n"
        f"어떻게 진행할까요?"
    )

    return {
        "kind": "ask_user_question",
        "questions": [
            {
                "header": "분해 확인",
                "question": question_text,
                "multi_select": False,
                "allow_other": False,
                "options": [
                    {"label": "이대로 진행", "description": "현재 분해 그대로 위임 시작"},
                    {"label": "더 세분화", "description": "todo 비우고 planner 에게 더 작은 단위로 재분해 요청"},
                    {"label": "더 통합", "description": "todo 비우고 planner 에게 더 큰 단위로 재분해 요청"},
                ],
            }
        ],
    }


def _extract_decomposition_answer(answer: Any) -> str:
    """Pull the user's selection out of the resume payload.

    CLI returns dict keyed by question header (``{"분해 확인": "이대로 진행"}``).
    Optional fallbacks accept list-of-dict and bare strings for tests / programmatic
    resume.
    """
    if isinstance(answer, dict):
        if "분해 확인" in answer:
            v = answer["분해 확인"]
        elif answer:
            v = next(iter(answer.values()))
        else:
            v = ""
        if isinstance(v, list):
            v = v[0] if v else ""
        return str(v or "").strip()
    if isinstance(answer, list) and answer:
        first = answer[0]
        if isinstance(first, dict):
            return str(first.get("value") or first.get("answer") or "").strip()
        if isinstance(first, str):
            return first.strip()
    if isinstance(answer, str):
        return answer.strip()
    return ""


def _classify_decomposition_answer(value: str) -> str:
    """Map a free-form answer string to one of: ``proceed``, ``finer``,
    ``coarser``, ``unknown``. Pure helper for snapshot tests.
    """
    if not value:
        return "unknown"
    if "세분화" in value:
        return "finer"
    if "통합" in value:
        return "coarser"
    if "이대로" in value or "그대로" in value or value.startswith("진행"):
        return "proceed"
    return "unknown"


def _nudge_decision(
    counts: dict[str, int],
    last_unfinished: int | None,
    stuck_nudges: int,
    max_stuck: int,
) -> str:
    """Decide between nudge / stuck_end / clean_end after a silent-terminate.

    - ``clean_end``   — ledger empty (pending+in_progress == 0). Let the
      orchestrator finish normally.
    - ``nudge``       — pending exists AND either (a) no prior nudge or
      (b) progress since last nudge (unfinished decreased). Reset the
      stuck counter in the caller.
    - ``stuck_end``   — pending exists but no progress since last nudge and
      the stuck counter has reached ``max_stuck``. Gives up re-prompting.

    The "progress-based reset" is what separates this from a pure accumulator:
    qwen3-max style models silent-terminate after every completed batch, so a
    cumulative cap (e.g. 3 total) runs out mid-run. Resetting on progress lets
    the harness keep enforcing the rule as long as the model is actually
    making forward progress, and only abort when truly stuck.
    """
    unfinished = counts.get("pending", 0) + counts.get("in_progress", 0)
    if unfinished == 0:
        return "clean_end"
    progress_made = last_unfinished is None or unfinished < last_unfinished
    if progress_made:
        return "nudge"
    if stuck_nudges >= max_stuck:
        return "stuck_end"
    return "nudge"


def _build_pending_nudge_message(
    first_item: Any,
    counts: dict[str, int],
) -> str:
    """Render the HumanMessage content injected when nudging.

    ``first_item`` is the next pending/in_progress :class:`TodoItem` (or None
    if the ledger was cleared between the route decision and the nudge node).
    Kept as a pure function so tests can snapshot the output.
    """
    pending = counts.get("pending", 0)
    in_progress = counts.get("in_progress", 0)
    if first_item is not None:
        return (
            f"⚠ Termination blocked: ledger 에 pending={pending}, "
            f"in_progress={in_progress} 작업이 남아있습니다. "
            f"자연어 요약으로 끝내지 말고, 지금 바로 `task` 도구를 호출해서 "
            f"첫 번째 항목을 진행하세요:\n\n"
            f"    {first_item.id}: {first_item.content}\n\n"
            f"coder / verifier / fixer 중 적합한 agent_type 을 골라 "
            f"task description 첫 줄에 `{first_item.id}: ...` 을 포함시키면 "
            f"harness 가 자동으로 in_progress/completed 를 마킹합니다."
        )
    return (
        "⚠ Termination blocked: ledger 에 처리되지 않은 todo 가 "
        "남아있습니다. task 도구를 호출해서 진행하세요."
    )


def _render_ledger_snapshot(todo_store: "TodoStore") -> str:
    """Compact ledger view injected into the orchestrator system prompt.

    Keeps the orchestrator aware of pending/in_progress/completed counts and
    the first few actionable rows so it can judge the termination condition
    without holding write_todos/update_todo directly.
    """
    items = todo_store.list_items()
    if not items:
        return "## Ledger snapshot\n(empty — no todos registered yet)"
    counts = todo_store.counts()
    lines = [
        "## Ledger snapshot",
        (
            f"totals: pending={counts.get('pending', 0)}, "
            f"in_progress={counts.get('in_progress', 0)}, "
            f"completed={counts.get('completed', 0)} "
            f"(of {len(items)})"
        ),
    ]
    status_glyph = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    for it in items:
        lines.append(f"  {status_glyph.get(it.status, '[?]')} {it.id}: {it.content}")
    return "\n".join(lines)


SYSTEM_PROMPT = """당신은 Orchestrator AI Coding Agent입니다.
직접 코드를 작성하거나 작업을 분해·설계하지 않고, task 도구로 전문
SubAgent에게 위임합니다. orchestrator 의 책임은 SubAgent 간 조율과
진행 관리입니다 — 요구사항 분석·task 분해·todo 등록·산출물 설계는
모두 planner 에게 위임하세요.

## 사용 가능한 도구
- read_file / glob_files / grep: 결과물 확인용
- task: SubAgent 위임 (코드 작성/수정/실행 모두 이 경로)

## SubAgent 역할
- planner: 요구사항 분석, PRD/SPEC 등 기획 산출물 작성, task 분해, **todo 초기 등록 (write_todos 직접 호출)**
- coder: 코드 작성·수정·실행
- verifier: 테스트/빌드 검증 (수정 금지)
- fixer: 지정된 실패 지점을 타겟팅해 수정
- reviewer: 코드 품질 검토
- researcher: 코드/문서 탐색

## 원칙
- 사용자가 요청하지 않은 기능·산출물·도구를 임의로 추가하지 마세요.
- 사용자가 명시하지 않은 기술·아키텍처·산출물 포맷을 todo 제목·설명이나
  task 위임 description 에 포함하면 안 됩니다. 그런 결정이 필요하면
  planner 가 확인 후 산출물에 반영하게 하세요.
- 사용자 요청에 요구사항 모호성(구현 범위·산출물 포맷·기술 스택 미지정
  등)이 있으면 task 위임 전에 planner 에게 위임해 ask_user_question 으로
  확정하세요. harness 는 빠진 부분을 임의로 채우지 않습니다.
- 어떤 SubAgent에게 무엇을 어떤 순서로 위임할지, 얼마나 나눠서 위임할지,
  어떤 산출물을 먼저 만들지는 모두 orchestrator인 당신이 판단합니다.
  harness 는 특정 워크플로·산출물 형식·섹션 구조를 강제하지 않습니다.

## Todo 운용 (v22.2 변경)
- todo 초기 등록은 **planner 가 직접** write_todos 로 합니다. orchestrator 는
  planner 위임 시 "task 분해 후 write_todos 로 등록까지" 명시하세요.
- 등록 순서 = 작업 순서. pending 첫 항목부터 coder 에게 위임하세요.
- task description 첫 줄에 `TASK-NN: ...` 을 포함하면 harness 가 자동으로
  in_progress/completed 마킹합니다 (auto-advance). 별도 ledger 호출 불필요.
- v22.1 부터 coder COMPLETED 후 자동으로 verifier 가 호출되며, 실패 시
  fixer 사이클 (최대 3회) 이 task_tool 안에서 atomic 으로 진행됩니다.
  결과 본문에 `[AUTO_VERIFY_PASSED]` 또는 `[AUTO_VERIFY_FAILED]` 마커가
  붙습니다. FAILED 면 그 task 만 사용자 검토 영역으로 두고 다음 pending
  task 로 진행하세요 — fixer 를 *추가로* 호출하면 hard-cap 에 걸려 차단
  됩니다.

## 종료
- 아래 ledger snapshot 의 pending 과 in_progress 가 모두 0 이면, 즉시
  자연어 요약 한 번으로 응답을 마무리하세요. 새 task 위임을 만들면 안
  됩니다.
- todo ledger 를 쓰지 않는 짧은 요청의 경우에도, 사용자 요청이 충족되면
  동일하게 자연어 요약으로 종료합니다.

{user_decisions_block}

{ledger_snapshot}

{memory_context}
"""


# 분해 확인은 harness 가 직접 처리한다. SYSTEM_PROMPT 에서 "ledger 직후
# ask_user_question 호출" 같은 행동 지시는 *전부 삭제됨* — gate_decomposition_node
# 가 LangGraph interrupt() 로 사용자에게 직접 묻고 답을 분기 처리.
# v6/v7/v8/v9 누적 회귀 (prompt fidelity 의존) 의 근본 원인 제거.


def _build_user_decisions_block(decisions_header: str) -> str:
    """orchestrator SYSTEM_PROMPT 상단용. SubAgent 도 같은 블록을 본다 —
    한 곳에 모아두면 orchestrator 가 사용자 답변을 인지 못하고 같은 질문을
    반복하는 회귀 (v7) 가 막힌다. header 가 비어 있으면 빈 줄.
    """
    return decisions_header if decisions_header else ""


class AgentLoop:
    """메인 에이전트 루프를 구성하고 실행한다."""

    def __init__(self) -> None:
        cfg = get_config()

        # 메모리 시스템 — minyoung_mah.SqliteMemoryStore 기반.
        self._store = MemoryStore(cfg.memory_db_path, tiers=["user", "project", "domain"])
        self._extractor = MemoryExtractor(get_model("fast"))
        self._memory_mw = MemoryMiddleware(self._store, self._extractor)

        # SubAgent 시스템 — minyoung_mah.Orchestrator 기반.
        self._user_decisions = UserDecisionsLog()
        self._todo_store = TodoStore()
        self._todo_change_callback: Any | None = None
        # ledger SubAgent 가 write_todos/update_todo 도구를 소유 — orchestrator
        # 직접 바인딩은 제거됨. todo_store 를 factory 에 넘겨 adapter 등록.
        self._orchestrator = build_orchestrator(
            memory_store=self._store,
            user_decisions=self._user_decisions,
            todo_store=self._todo_store,
            todo_change_callback=lambda items: (
                self._todo_change_callback(items)
                if self._todo_change_callback
                else None
            ),
        )

        # Context compaction — token-aware threshold + LLM summarize.
        # 옛 _trim_orchestrator_messages (단순 message-count 슬라이스) 의 정보
        # 손실 + Anthropic strict pair 위반 결함 해소. claude-code 의
        # autoCompact 패턴을 minyoung_mah.context 로 승격.
        self._context_manager = ContextManager(
            policy=default_policy(),
            compact_model=get_model("fast"),
            observer=self._orchestrator.observer,
        )

        # 복원력 시스템
        self._watchdog = Watchdog(timeout_sec=cfg.llm_timeout)
        self._progress_guard = ProgressGuard(
            max_iterations=cfg.max_iterations,
            key_extractor=_task_id_extractor,
        )
        self._safe_stop = SafeStop()
        self._error_handler = ErrorHandler(fallback_enabled=True)

        # 도구
        task_tool = build_task_tool(
            self._orchestrator,
            self._user_decisions,
            todo_store=self._todo_store,
            todo_change_callback=lambda items: (
                self._todo_change_callback(items)
                if self._todo_change_callback
                else None
            ),
        )

        # Orchestrator 에는 읽기 전용 도구 + task 위임 도구만 바인딩.
        # write_todos/update_todo 는 ledger SubAgent 전용 — orchestrator 가 직접
        # ledger 를 조작하면 "선제적 task 분해" 현상이 발생해 도구 권한 수준에서
        # 차단한다. write_file/edit/execute 도 SubAgent 전용.
        from coding_agent.tools.file_ops import read_file, glob_files, grep
        self._tools = [read_file, glob_files, grep, task_tool]

        # 그래프
        self._graph = self._build_graph()

    def _build_graph(self):
        """LangGraph StateGraph를 구성한다."""
        tools = self._tools
        tool_node = ToolNode(tools)
        tool_prompt_block = build_tool_prompt(tools)

        # 모델 적응적 바인딩 캐시
        _model_cache: dict[str, tuple] = {}

        def get_bound_model(tier: str = "strong"):
            """모델의 tool calling 지원 여부에 따라 적응적으로 바인딩.

            Returns: (model, use_prompt_tools: bool)
            """
            if tier in _model_cache:
                return _model_cache[tier]

            model = get_model(tier, temperature=0.0)
            model_name = get_model_name(tier)
            bound, use_prompt = bind_tools_adaptive(model, tools, model_name)
            _model_cache[tier] = (bound, use_prompt)
            return bound, use_prompt

        # ── 노드 정의 ──

        async def inject_memory(state: AgentState) -> dict[str, Any]:
            """메모리 주입 + 사용자 입력에서 메모리 추출 노드.

            메모리 추출은 사용자 입력이 있는 이 시점에서만 수행한다.
            루프 중간(도구 실행 후)에는 추출하지 않는다 — 거기에는
            사용자 정보가 아닌 에이전트의 자체 결정만 있기 때문이다.
            """
            t0 = time.monotonic()

            # 1) 사용자 입력에서 메모리 추출 (첫 진입 시에만)
            if "iteration" not in state or state.get("iteration") is None:
                await self._memory_mw.extract_and_store(state)

            # 2) 메모리 주입
            result = await self._memory_mw.inject(state)
            updates: dict[str, Any] = {
                "memory_context": result.get("memory_context", ""),
            }
            if "iteration" not in state or state.get("iteration") is None:
                cfg = get_config()
                updates["iteration"] = 0
                updates["max_iterations"] = cfg.max_iterations
                updates["current_tier"] = cfg.orchestrator_tier
                updates["stall_count"] = 0
            log.debug("timing.inject_memory", elapsed_s=round(time.monotonic() - t0, 3))
            return updates

        async def agent_node(state: AgentState) -> dict[str, Any]:
            """LLM 호출 노드.

            오픈소스 모델 호환성:
            1. native tool calling 지원 → bind_tools 사용
            2. 미지원 (GLM, MiniMax 등) → 프롬프트에 도구 스키마 주입,
               텍스트 응답에서 tool_call JSON 블록 파싱
            3. 메시지 전처리: 고아 tool_call 정리, DashScope 직렬화 보장
            4. Context compaction: minyoung_mah.context.ContextManager 가
               token-aware threshold 도달 시 LLM summarize 로 대체. 단순
               message-count 슬라이스 (옛 _trim_orchestrator_messages) 의
               정보 손실 + Anthropic strict pair 위반 결함 해소.
            """
            t0 = time.monotonic()
            tier = state.get("current_tier") or get_config().orchestrator_tier
            iteration = (state.get("iteration") or 0) + 1
            model, use_prompt_tools = get_bound_model(tier)

            messages = list(state.get("messages", []))
            # Token-aware context compaction. 임계값 미달이면 원본 그대로
            # 반환 (compacted=False). 도달하면 별도 LLM 으로 summarize +
            # boundary marker + summary message 로 교체. 정보 보존.
            if self._context_manager is not None:
                compact_result = await self._context_manager.compact_if_needed(
                    messages, model
                )
                messages = compact_result.messages

            # 시스템 프롬프트 구성
            memory_ctx = state.get("memory_context", "")
            ledger_snapshot = _render_ledger_snapshot(self._todo_store)
            user_decisions_block = _build_user_decisions_block(
                self._user_decisions.header()
            )
            sys_prompt = SYSTEM_PROMPT.format(
                memory_context=memory_ctx,
                ledger_snapshot=ledger_snapshot,
                user_decisions_block=user_decisions_block,
            )

            # 프롬프트 기반 도구 호출 모드면 도구 스키마를 시스템 프롬프트에 추가
            if use_prompt_tools:
                sys_prompt += "\n" + tool_prompt_block

            # 시스템 메시지 설정
            if not messages or not isinstance(messages[0], SystemMessage):
                messages.insert(0, SystemMessage(content=sys_prompt))
            else:
                messages[0] = SystemMessage(content=sys_prompt)

            # 메시지 전처리 (고아 정리, 직렬화 보장)
            t_prep = time.monotonic()
            messages = prepare_messages_for_llm(messages)
            prep_elapsed = time.monotonic() - t_prep

            try:
                model_name = get_model_name(tier)
                t_llm = time.monotonic()
                response = invoke_with_tool_fallback(
                    model=model,
                    messages=messages,
                    tools=tools,
                    model_name=model_name,
                    use_prompt_tools=use_prompt_tools,
                )
                llm_elapsed = time.monotonic() - t_llm

                # tool call 요약
                tool_names = []
                if hasattr(response, "tool_calls") and response.tool_calls:
                    tool_names = [tc.get("name", "?") for tc in response.tool_calls]

                log.info(
                    "timing.agent_node",
                    iteration=iteration,
                    tier=tier,
                    prep_s=round(prep_elapsed, 3),
                    llm_s=round(llm_elapsed, 3),
                    total_s=round(time.monotonic() - t0, 3),
                    msg_count=len(messages),
                    tool_calls=tool_names or None,
                )

                return {
                    "messages": [response],
                    "iteration": iteration,
                }
            except Exception as e:
                log.error(
                    "agent_node.error",
                    error=str(e),
                    tier=tier,
                    elapsed_s=round(time.monotonic() - t0, 3),
                )
                return {
                    "error_info": {
                        "error": str(e),
                        "exception": e,
                        "step": "agent_node",
                    },
                    "iteration": iteration,
                }

        async def extract_memory(state: AgentState) -> dict[str, Any]:
            """턴 종료 후 메모리 추출 노드."""
            t0 = time.monotonic()
            result = await self._memory_mw.extract_and_store(state)
            log.debug("timing.extract_memory", elapsed_s=round(time.monotonic() - t0, 3))
            return result

        def check_progress(state: AgentState) -> dict[str, Any]:
            """진전 감시 노드."""
            nonlocal _consecutive_errors
            _consecutive_errors = 0  # 도구 실행 성공 → 에러 카운터 리셋

            messages = state.get("messages", [])
            iteration = state.get("iteration", 0)

            # 가장 최근 AIMessage(도구 호출이 들어 있는)를 찾아 record.
            # check_progress는 ToolNode 다음에 실행되므로 messages[-1]은
            # 항상 ToolMessage이고 tool_calls가 비어 있다. AIMessage는 그
            # 직전(또는 그보다 앞)에 있다. tool_calls가 있는 첫 메시지를
            # 역방향으로 찾는다.
            for msg in reversed(messages):
                tcs = getattr(msg, "tool_calls", None)
                if tcs:
                    for tc in tcs:
                        self._progress_guard.record_action(
                            tc.get("name", "unknown"),
                            tc.get("args", {}),
                        )
                    break

            verdict = self._progress_guard.check(iteration)

            result: dict[str, Any] = {}

            # 분해 확인 implicit detection 은 더 이상 사용하지 않는다 —
            # gate_decomposition_node 가 LangGraph interrupt() 로 사용자에게
            # 직접 묻고 답을 분기 처리한다. confirmed 플래그는 그 분기에서만
            # set 된다.

            if verdict == GuardVerdict.STOP:
                result.update(
                    {
                        "exit_reason": "progress_guard_stop",
                        "stall_count": (state.get("stall_count") or 0) + 1,
                    }
                )
                return result
            elif verdict == GuardVerdict.WARN:
                log.warning("progress_guard.stall_detected", iteration=iteration)
                result["stall_count"] = (state.get("stall_count") or 0) + 1
                return result

            return result

        _consecutive_errors = 0
        _MAX_CONSECUTIVE_ERRORS = 3
        # Termination-with-pending-todos 재시도 — "진전 없는 연속 silent-
        # terminate" 횟수 상한. 직전 nudge 대비 unfinished 가 줄어들면 리셋
        # 되므로, 진전이 있는 한 ledger 가 빌 때까지 계속 찌른다. 3 은 "모델
        # 이 세 번 연속 찔러도 같은 ledger 상태로 자연어만 뱉는" 정체 신호.
        # 2026-04-21 v1 E2E (46 tasks) 관찰 — qwen3-max 가 매 배치 후
        # silent-terminate 하는 패턴이므로 누적 상한(3)은 부족. progress-based
        # reset 으로 보완.
        _MAX_STUCK_NUDGES = 3

        def handle_error(state: AgentState) -> dict[str, Any]:
            """에러 처리 노드.

            같은 에러가 연속으로 _MAX_CONSECUTIVE_ERRORS번 발생하면
            재시도하지 않고 즉시 안전 중단한다.
            """
            nonlocal _consecutive_errors
            _consecutive_errors += 1

            error_info = state.get("error_info", {})
            error = error_info.get("exception") or Exception(
                error_info.get("error", "unknown")
            )

            # 연속 에러 한도 초과 → 즉시 중단
            if _consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                log.error(
                    "error_handler.consecutive_limit",
                    count=_consecutive_errors,
                    error=str(error)[:200],
                )
                return {
                    "error_info": {},
                    "exit_reason": f"consecutive_errors_{_consecutive_errors}",
                }

            resolution = self._error_handler.handle(error, state)

            log.info(
                "error_handler.resolution",
                action=resolution.action,
                status=resolution.status_message,
                consecutive=_consecutive_errors,
            )

            result: dict[str, Any] = {"error_info": {}}

            if resolution.action == "retry":
                result["retry_count_for_this_error"] = resolution.metadata.get(
                    "retry_count", 0
                )
            elif resolution.action == "fallback":
                next_tier = resolution.metadata.get("next_tier")
                if next_tier:
                    result["current_tier"] = next_tier
                    result["retry_count_for_this_error"] = 0
                else:
                    result["exit_reason"] = "all_models_exhausted"
            elif resolution.action == "abort":
                result["exit_reason"] = "error_abort"

            return result

        def nudge_pending_todos_node(state: AgentState) -> dict[str, Any]:
            """Inject a reminder when orchestrator tries to terminate with
            pending todos still on the ledger.

            Qwen3-max observed in v12/v1 E2E: after each batch of completed
            tasks, orchestrator emits a natural-language progress report with
            ``tool_calls=None`` ("다음 작업을 진행할 준비가 되어 있습니다")
            despite the system prompt stating pending > 0 cannot terminate.
            Pattern repeats every batch, so a cumulative nudge cap runs out
            mid-run. This node resets the stuck counter when unfinished
            decreases versus the last nudge — progress-based, not cumulative.
            """
            items = self._todo_store.list_items()
            counts = self._todo_store.counts()
            unfinished = counts.get("pending", 0) + counts.get("in_progress", 0)
            first = next(
                (it for it in items if it.status in ("pending", "in_progress")),
                None,
            )
            last_unfinished = state.get("last_nudge_unfinished")
            progress_made = last_unfinished is None or unfinished < last_unfinished
            new_nudges = 1 if progress_made else (state.get("pending_nudges") or 0) + 1
            reminder = _build_pending_nudge_message(first, counts)
            log.warning(
                "orchestrator.pending_nudge_injected",
                pending=counts.get("pending", 0),
                in_progress=counts.get("in_progress", 0),
                first_task=(first.id if first else None),
                nudge_count=new_nudges,
                progress_reset=progress_made,
                last_unfinished=last_unfinished,
                unfinished=unfinished,
            )
            return {
                "messages": [HumanMessage(content=reminder)],
                "pending_nudges": new_nudges,
                "last_nudge_unfinished": unfinished,
            }

        def gate_decomposition_node(state: AgentState) -> dict[str, Any]:
            """Harness-driven decomposition confirmation gate.

            Pauses the graph with LangGraph ``interrupt()`` and surfaces an
            ``ask_user_question``-shaped payload to the CLI. When the user
            answers, LangGraph re-runs this node from the top with the
            answer available to ``interrupt()``'s call site — we then
            translate the answer into state updates (set
            ``decomposition_confirmed``, optionally ``reset()`` the ledger
            and inject a HumanMessage that redirects the orchestrator to
            re-delegate to planner).

            Replaces the prompt-fidelity-dependent flow where the
            orchestrator was nudged to call ``ask_user_question`` itself.
            The harness now owns both the question and the branching logic,
            so the gate is enforced regardless of how well the model
            follows instructions (v6/v7/v8/v9 회귀 근본 원인 제거).
            """
            counts = self._todo_store.counts()
            items = self._todo_store.list_items()
            preview = [f"{it.id}: {it.content[:80]}" for it in items[:5]]

            # Identify the blocked tool_call to satisfy LangChain's
            # "every tool_call must have a corresponding ToolMessage" rule.
            messages = state.get("messages", [])
            last_msg = messages[-1] if messages else None
            blocked_id: str | None = None
            for tc in (getattr(last_msg, "tool_calls", None) or []):
                if tc.get("name") == "task":
                    args = tc.get("args") or {}
                    role = (args.get("agent_type") or "").strip().lower()
                    if role in _GATED_ROLES:
                        blocked_id = tc.get("id")
                        break

            payload = _build_decomposition_interrupt_payload(counts, preview)
            log.info(
                "orchestrator.decomposition_gate_interrupt",
                pending=counts.get("pending", 0),
                in_progress=counts.get("in_progress", 0),
                completed=counts.get("completed", 0),
                total=sum(counts.values()),
                blocked_tool_call_id=blocked_id,
            )

            # ``interrupt()`` raises ``GraphInterrupt`` on first entry; on
            # resume via ``Command(resume=...)`` it returns the supplied
            # value here. LangGraph re-executes the node from the top on
            # resume, so the lines above run twice — by design.
            answer = interrupt(payload)
            value = _extract_decomposition_answer(answer)
            decision = _classify_decomposition_answer(value)

            updates: dict[str, Any] = {"decomposition_confirmed": True}
            inject_messages: list[Any] = []

            if decision == "finer":
                # Clear ledger and steer planner to a finer breakdown.
                self._todo_store.reset()
                if self._todo_change_callback:
                    try:
                        self._todo_change_callback(self._todo_store.list_items())
                    except Exception:  # noqa: BLE001
                        pass
                if blocked_id is not None:
                    inject_messages.append(
                        ToolMessage(
                            content=(
                                "사용자 답변: '더 세분화'. 차단된 위임은 취소되었고 "
                                "ledger 가 비워졌습니다. planner 에게 더 작은 단위 "
                                "(예: 기존의 1.5~2배 task 수) 로 재분해를 요청하세요."
                            ),
                            tool_call_id=blocked_id,
                        )
                    )
                inject_messages.append(
                    HumanMessage(
                        content=(
                            f"사용자가 task 분해를 더 세분화해달라고 요청했습니다 "
                            f"(이전 분해: {sum(counts.values())}개). ledger 는 이미 "
                            f"비워졌습니다. planner 에게 더 작은 단위로 재분해 (예: "
                            f"기존의 1.5~2배 task 수) 를 요청하고, 결과를 ledger 에 "
                            f"다시 등록하세요."
                        )
                    )
                )
            elif decision == "coarser":
                self._todo_store.reset()
                if self._todo_change_callback:
                    try:
                        self._todo_change_callback(self._todo_store.list_items())
                    except Exception:  # noqa: BLE001
                        pass
                if blocked_id is not None:
                    inject_messages.append(
                        ToolMessage(
                            content=(
                                "사용자 답변: '더 통합'. 차단된 위임은 취소되었고 "
                                "ledger 가 비워졌습니다. planner 에게 더 큰 단위로 "
                                "재분해를 요청하세요."
                            ),
                            tool_call_id=blocked_id,
                        )
                    )
                inject_messages.append(
                    HumanMessage(
                        content=(
                            f"사용자가 task 분해를 더 통합해달라고 요청했습니다 "
                            f"(이전 분해: {sum(counts.values())}개). ledger 는 이미 "
                            f"비워졌습니다. planner 에게 더 큰 단위 (예: 기존의 절반 "
                            f"task 수) 로 재분해를 요청하고, 결과를 ledger 에 다시 "
                            f"등록하세요."
                        )
                    )
                )
            else:
                # ``proceed`` or ``unknown`` — pass the original delegation
                # through. We still need to satisfy the blocked tool_call.
                if blocked_id is not None:
                    note = (
                        "사용자 답변: '이대로 진행'. 차단된 위임을 그대로 재시도하세요."
                        if decision == "proceed"
                        else (
                            f"사용자 답변 ({value!r}) 을 명확히 분류하지 못했습니다. "
                            f"보수적으로 차단된 위임을 그대로 재시도합니다."
                        )
                    )
                    inject_messages.append(
                        ToolMessage(content=note, tool_call_id=blocked_id)
                    )

            if inject_messages:
                updates["messages"] = inject_messages

            log.info(
                "orchestrator.decomposition_gate_resolved",
                decision=decision,
                raw_answer=value[:120],
            )
            return updates

        # ── Sufficiency loop nodes ──
        # apt-legal 패턴 이식. ``Config.sufficiency_enabled=True`` 일 때만
        # ``route_after_agent`` 가 ``sufficiency_gate`` 로 분기한다. 비활성
        # 모드에서는 노드는 등록만 되고 도달하지 않는다.

        def sufficiency_gate_node(state: AgentState) -> dict[str, Any]:
            """Run the deterministic rule_gate.

            HIGH 면 종료, LOW 면 휴리스틱 verdict 를 미리 채워
            ``sufficiency_apply_node`` 에 넘김, MEDIUM 이면 critic 호출로
            라우팅된다 (라우팅 함수가 결정).
            """
            cfg = get_config()
            signals = _suff_signals.collect_signals(dict(state), self._todo_store)
            gate = _suff_rules.evaluate(
                signals,
                high_todo=cfg.sufficiency_high_todo,
                low_todo=cfg.sufficiency_low_todo,
                high_prd=cfg.sufficiency_high_prd,
                low_prd=cfg.sufficiency_low_prd,
            )
            log.info(
                "sufficiency.gate",
                level=gate.level,
                triggered=gate.triggered_signals,
                metrics=gate.metrics,
            )
            updates: dict[str, Any] = {
                "last_critic_verdict": {
                    "_gate_level": gate.level,
                    "_gate_metrics": gate.metrics,
                    "_gate_reason": gate.reason,
                    "_gate_triggered": list(gate.triggered_signals),
                },
            }
            if gate.level == "LOW":
                # LOW 분기는 critic 비용 없이 휴리스틱 verdict 로 직행
                verdict = _suff_rules.heuristic_verdict_for_low(gate)
                updates["last_critic_verdict"] = {
                    **updates["last_critic_verdict"],
                    **_suff_loop.serialize_verdict(verdict),
                }
            return updates

        async def critic_node(state: AgentState) -> dict[str, Any]:
            """Invoke the LLM critic for MEDIUM band gates."""
            stash = state.get("last_critic_verdict") or {}
            metrics = stash.get("_gate_metrics") or {}
            iteration = (state.get("sufficiency_iterations") or 0) + 1

            # 사용자 원 요청은 첫 HumanMessage 에서 추출
            user_request = ""
            for m in state.get("messages", []) or []:
                if isinstance(m, HumanMessage):
                    content = m.content if isinstance(m.content, str) else ""
                    if content:
                        user_request = content
                        break

            verdict = await _critic_mod.invoke_critic(
                self._orchestrator,
                user_request=user_request,
                metrics=metrics,
                iteration=iteration,
            )
            log.info(
                "sufficiency.critic.done",
                verdict=verdict.verdict,
                target_role=verdict.target_role,
                iteration=iteration,
            )
            return {
                "last_critic_verdict": {
                    **stash,
                    **_suff_loop.serialize_verdict(verdict),
                },
            }

        async def sufficiency_apply_node(state: AgentState) -> dict[str, Any]:
            """Apply the verdict — emit observer event, push history,
            either inject feedback HumanMessage (retry/replan) or notify HITL
            and mark ``needs_human_review`` (escalate). pass falls through to
            ``extract_memory_final`` via the router.
            """
            cfg = get_config()
            stash = dict(state.get("last_critic_verdict") or {})
            gate_level = stash.pop("_gate_level", "MEDIUM")
            gate_metrics = stash.pop("_gate_metrics", {}) or {}
            stash.pop("_gate_reason", None)
            stash.pop("_gate_triggered", None)

            iteration = (state.get("sufficiency_iterations") or 0) + 1

            # Reconstruct the CriticVerdict from the serialized payload
            verdict = _CriticVerdict(
                verdict=stash.get("verdict", "escalate_hitl"),
                target_role=stash.get("target_role"),
                reason=stash.get("reason", "(reason 누락)"),
                feedback_for_retry=stash.get("feedback_for_retry"),
            )

            # Cycle detection — promotes retry/replan to escalate when blocked
            history_raw: list[dict[str, Any]] = list(
                state.get("sufficiency_history") or []
            )
            new_hash = _suff_loop.compute_cycle_hash(
                gate_level, verdict.verdict, verdict.target_role
            )
            is_cycle = _suff_loop.detect_cycle(history_raw, new_hash)
            verdict = _suff_loop.force_escalate_if_blocked(
                verdict,
                iteration=iteration,
                max_iterations=cfg.sufficiency_max_iterations,
                is_cycle=is_cycle,
            )

            # Append history entry (post-promotion so the recorded verdict
            # matches the actual decision applied)
            new_entry = _suff_loop.SufficiencyHistoryEntry(
                iteration=iteration,
                rule_level=gate_level,
                verdict=verdict.verdict,
                target_role=verdict.target_role,
                cycle_hash=new_hash,
            )
            history_raw.append(_suff_loop.serialize_history_entry(new_entry))

            # Standard observability events
            await _suff_loop.emit_critic_verdict_event(
                self._orchestrator.observer,
                verdict=verdict,
                iteration=iteration,
                rule_level=gate_level,
                metrics=gate_metrics,
            )

            updates: dict[str, Any] = {
                "sufficiency_iterations": iteration,
                "sufficiency_history": history_raw,
                "last_critic_verdict": _suff_loop.serialize_verdict(verdict),
            }

            if verdict.verdict == "escalate_hitl":
                await _suff_loop.notify_hitl_escalation(
                    self._orchestrator.hitl,
                    verdict=verdict,
                    iteration=iteration,
                    metrics=gate_metrics,
                )
                updates["needs_human_review"] = True
                updates["exit_reason"] = "sufficiency_escalated"
                log.warning(
                    "sufficiency.escalated",
                    iteration=iteration,
                    rule_level=gate_level,
                    reason=verdict.reason[:200],
                )
                return updates

            if verdict.verdict in ("retry_lookup", "replan"):
                feedback_text = _suff_loop.build_feedback_human_message(
                    verdict
                ).replace("{iter}", str(iteration))
                updates["messages"] = [HumanMessage(content=feedback_text)]
                # Reset pending_nudges so the next agent turn isn't immediately
                # killed by the stuck counter — feedback is itself the
                # progress signal.
                updates["pending_nudges"] = 0
                updates["last_nudge_unfinished"] = None
                log.info(
                    "sufficiency.retry",
                    iteration=iteration,
                    target_role=verdict.target_role,
                )
                return updates

            # pass — clear pending feedback and let the router send us to
            # extract_memory_final.
            log.info("sufficiency.pass", iteration=iteration, rule_level=gate_level)
            return updates

        def safe_stop_node(state: AgentState) -> dict[str, Any]:
            """안전 중단 노드. 진행 상태를 파일로 저장하여 이어서 작업 가능."""
            exit_reason = state.get("exit_reason", "safe_stop")
            log.info("safe_stop", reason=exit_reason)

            resume = {
                "last_step": "safe_stop",
                "iteration": state.get("iteration", 0),
                "exit_reason": exit_reason,
                "stall_summary": self._progress_guard.get_stall_summary(),
            }

            # 진행 상태를 .ax-agent/resume.json에 저장
            self._save_resume_state(state, exit_reason)

            return {
                "exit_reason": exit_reason,
                "resume_metadata": resume,
            }

        # ── 라우팅 함수 ──

        def route_after_agent(state: AgentState) -> str:
            """agent 노드 후 라우팅."""
            # 에러 발생 시
            if state.get("error_info"):
                return "handle_error"

            # 안전 중단 체크
            should_stop, reason = self._safe_stop.evaluate(state)
            if should_stop:
                return "safe_stop"

            # 도구 호출 여부 확인
            messages = state.get("messages", [])
            if messages:
                last_msg = messages[-1]
                if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                    # 분해 확인 게이트 — ledger 등록 후 첫 번째 non-ledger
                    # task 위임을 1회 차단해서 사용자 confirmation 유도.
                    gate_needed, _blocked_id = _requires_decomposition_gate(
                        last_msg,
                        self._todo_store.counts(),
                        state.get("decomposition_confirmed", False),
                    )
                    if gate_needed:
                        return "gate_decomposition"
                    return "tools"

            # 도구 호출 없음 = 응답 완료 — 단, ledger 에 pending 이 남아있으면
            # nudge 를 주입해서 orchestrator 가 종료 규칙을 지키도록 강제한다.
            # 진전이 있는 한 계속 찌르고, "진전 없는 연속 실패"가
            # _MAX_STUCK_NUDGES 에 도달한 경우에만 포기하고 정상 종료 경로로
            # 빠진다 (v1 E2E 관찰, Qwen3-max).
            counts = self._todo_store.counts()
            decision = _nudge_decision(
                counts,
                state.get("last_nudge_unfinished"),
                state.get("pending_nudges") or 0,
                _MAX_STUCK_NUDGES,
            )
            if decision == "nudge":
                return "nudge_pending_todos"
            if decision == "stuck_end":
                # stuck 은 nudge 시스템이 이미 "agent 가 진전 없이 종료를
                # 반복" 으로 판정한 상태. sufficiency 가 끼면 LOW band 로
                # 분류돼 의미 없는 retry/escalate 가 추가될 뿐이다 (v6 회귀:
                # ledger 등록 직후 4 회 silent_terminate → stuck_end →
                # sufficiency LOW + MAX_ITER=1 → 즉시 escalate). stuck 은
                # 다른 안전망의 영역이므로 sufficiency 우회.
                log.warning(
                    "orchestrator.pending_nudge_stuck_abort",
                    pending=counts.get("pending", 0),
                    in_progress=counts.get("in_progress", 0),
                    stuck_nudges=state.get("pending_nudges") or 0,
                    last_unfinished=state.get("last_nudge_unfinished"),
                )
                return "extract_memory_final"

            # Sufficiency loop — clean_end 시점에 한 번 더 충족도 평가.
            # 비활성 시 직전 동작 그대로 extract_memory_final 직행.
            if get_config().sufficiency_enabled:
                # 사이클 방지: 이미 escalate 결정된 상태면 더 평가하지 않고 종료
                last_verdict = state.get("last_critic_verdict") or {}
                if last_verdict.get("verdict") == "escalate_hitl":
                    return "extract_memory_final"
                return "sufficiency_gate"

            return "extract_memory_final"

        def route_after_sufficiency_gate(state: AgentState) -> str:
            """Pick the post-rule-gate destination.

            HIGH → 종료, MEDIUM → critic, LOW → apply (휴리스틱 verdict 가
            이미 ``last_critic_verdict`` 에 저장돼 있음).
            """
            stash = state.get("last_critic_verdict") or {}
            level = stash.get("_gate_level", "MEDIUM")
            if level == "HIGH":
                return "extract_memory_final"
            if level == "LOW":
                return "sufficiency_apply"
            return "critic"

        def route_after_sufficiency_apply(state: AgentState) -> str:
            """Pick the post-apply destination.

            verdict==pass / escalated → 종료. retry/replan → 다시 agent 노드
            로 돌아 다음 iteration 진입 (feedback HumanMessage 가 messages
            에 이미 추가됐다).
            """
            verdict_payload = state.get("last_critic_verdict") or {}
            verdict = verdict_payload.get("verdict", "escalate_hitl")
            if verdict in ("retry_lookup", "replan"):
                return "agent"
            return "extract_memory_final"

        def route_after_check(state: AgentState) -> str:
            """check_progress 후 라우팅."""
            if state.get("exit_reason"):
                return "safe_stop"
            return "agent"

        def route_after_error(state: AgentState) -> str:
            """handle_error 후 라우팅."""
            if state.get("exit_reason"):
                return "safe_stop"
            return "agent"

        # ── 그래프 구성 ──

        builder = StateGraph(AgentState)

        builder.add_node("inject_memory", inject_memory)
        builder.add_node("agent", agent_node)
        builder.add_node("tools", tool_node)
        builder.add_node("nudge_pending_todos", nudge_pending_todos_node)
        builder.add_node("gate_decomposition", gate_decomposition_node)
        builder.add_node("sufficiency_gate", sufficiency_gate_node)
        builder.add_node("critic", critic_node)
        builder.add_node("sufficiency_apply", sufficiency_apply_node)
        builder.add_node("extract_memory_final", extract_memory)
        builder.add_node("check_progress", check_progress)
        builder.add_node("handle_error", handle_error)
        builder.add_node("safe_stop", safe_stop_node)

        # 엣지
        builder.set_entry_point("inject_memory")
        builder.add_edge("inject_memory", "agent")

        builder.add_conditional_edges(
            "agent",
            route_after_agent,
            {
                "tools": "tools",
                "nudge_pending_todos": "nudge_pending_todos",
                "gate_decomposition": "gate_decomposition",
                "sufficiency_gate": "sufficiency_gate",
                "extract_memory_final": "extract_memory_final",
                "handle_error": "handle_error",
                "safe_stop": "safe_stop",
            },
        )

        builder.add_conditional_edges(
            "sufficiency_gate",
            route_after_sufficiency_gate,
            {
                "critic": "critic",
                "sufficiency_apply": "sufficiency_apply",
                "extract_memory_final": "extract_memory_final",
            },
        )

        # critic 은 결정만 만들고 곧장 apply 로
        builder.add_edge("critic", "sufficiency_apply")

        builder.add_conditional_edges(
            "sufficiency_apply",
            route_after_sufficiency_apply,
            {
                "agent": "agent",
                "extract_memory_final": "extract_memory_final",
            },
        )

        # Nudge 후 곧바로 agent 재호출 — injected HumanMessage 를 읽고
        # 이번엔 task 도구를 호출하도록 재촉한다.
        builder.add_edge("nudge_pending_todos", "agent")

        # 분해 게이트 후 agent 재호출 — 안내 ToolMessage 를 읽고 이번엔
        # ask_user_question 을 호출하도록 유도.
        builder.add_edge("gate_decomposition", "agent")

        # 루프 중간에는 메모리 추출 없이 바로 진전 확인
        builder.add_edge("tools", "check_progress")

        builder.add_conditional_edges(
            "check_progress",
            route_after_check,
            {"agent": "agent", "safe_stop": "safe_stop"},
        )

        builder.add_conditional_edges(
            "handle_error",
            route_after_error,
            {"agent": "agent", "safe_stop": "safe_stop"},
        )

        builder.add_edge("extract_memory_final", END)
        builder.add_edge("safe_stop", END)

        # InMemorySaver enables LangGraph interrupt() — required by the
        # ask_user_question tool path. The checkpointer also gives us a
        # thread-scoped resume capability for free.
        return builder.compile(checkpointer=InMemorySaver())

    async def run(
        self,
        user_message: str,
        project_id: str | None = None,
        ask_user: "AskUserCallback | None" = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        """사용자 메시지를 처리하고 최종 상태를 반환한다.

        ``ask_user`` is an optional async callback used to satisfy
        ``ask_user_question`` interrupts. It receives the interrupt
        payload (a dict produced by the tool) and must return the
        user's answer (any JSON-serializable value). If omitted and
        an interrupt fires, the run aborts with exit_reason='no_ask_user_handler'.

        ``thread_id`` lets the caller pin the LangGraph checkpointer thread
        so consecutive turns from the same conversation accumulate state.
        Defaults to a per-call uuid (legacy behavior — independent turns).

        ``thread_id`` 를 명시하면 같은 thread 의 conversation state 가 누적.
        미지정 시 호출마다 새 uuid (기존 동작 — 독립 turn).
        """
        self._progress_guard.reset()

        initial_state: dict[str, Any] = {
            "messages": [HumanMessage(content=user_message)],
            "project_id": project_id or get_config().project_id or "",
            "working_directory": get_config().project_root.as_posix(),
        }

        # 명시 thread_id 가 있으면 같은 conversation 의 state (memory, todo,
        # 진행 상황) 가 누적된다. 없으면 매 turn 독립 (기존 cli 동작).
        # Provided thread_id pins the conversation; otherwise per-call uuid
        # keeps turns isolated (legacy CLI behavior).
        if thread_id is None:
            thread_id = f"orch-{uuid.uuid4()}"
        config = {
            "recursion_limit": 500,
            "configurable": {"thread_id": thread_id},
        }

        log.info("agent_loop.start", message_length=len(user_message), thread_id=thread_id)

        try:
            final_state = await self._graph.ainvoke(initial_state, config=config)

            # ── Interrupt-resume loop ──
            # If a node called interrupt() (typically from ask_user_question
            # propagated through task_tool), the result contains __interrupt__.
            # We hand each interrupt to ask_user, then resume with Command.
            while final_state and final_state.get("__interrupt__"):
                if ask_user is None:
                    log.warning(
                        "agent_loop.interrupt_without_handler",
                        thread_id=thread_id,
                    )
                    final_state["exit_reason"] = "no_ask_user_handler"
                    break

                interrupts = final_state["__interrupt__"]
                # LangGraph reports interrupts as a tuple/list of Interrupt objects.
                first = interrupts[0] if isinstance(interrupts, (list, tuple)) else interrupts
                payload = getattr(first, "value", first)

                log.info("agent_loop.interrupt", payload_type=type(payload).__name__)
                answer = await ask_user(payload)
                log.info("agent_loop.interrupt_resumed", answer_preview=str(answer)[:80])

                final_state = await self._graph.ainvoke(
                    Command(resume=answer),
                    config=config,
                )
        except Exception as e:
            log.error("agent_loop.fatal_error", error=str(e))
            final_state = {
                "exit_reason": "fatal_error",
                "error_info": {"error": str(e)},
                "messages": [
                    HumanMessage(content=user_message),
                    AIMessage(content=f"치명적 오류가 발생했습니다: {e}"),
                ],
            }

        # 최종 응답 추출
        messages = final_state.get("messages", [])
        final_response = ""
        if messages:
            last = messages[-1]
            if hasattr(last, "content"):
                final_response = last.content if isinstance(last.content, str) else str(last.content)

        final_state["final_response"] = final_response

        log.info(
            "agent_loop.complete",
            iterations=final_state.get("iteration", 0),
            exit_reason=final_state.get("exit_reason", "completed"),
        )

        # SubAgent 정리 — minyoung_mah.Orchestrator 는 stateless 라 별도
        # cleanup 불필요. (기존 manager.cleanup 은 in-memory 인스턴스 gc 목적이었음.)

        return final_state

    def get_memory_store(self) -> MemoryStore:
        """메모리 스토어 인스턴스 반환 (CLI 용)."""
        return self._store

    def get_orchestrator(self):
        """minyoung_mah Orchestrator 인스턴스 반환 (CLI 용)."""
        return self._orchestrator

    def get_todo_store(self) -> TodoStore:
        """Todo ledger 인스턴스 반환 (CLI 용)."""
        return self._todo_store

    def set_todo_change_callback(self, callback) -> None:
        """CLI 가 rendered panel 을 갱신하도록 콜백 등록."""
        self._todo_change_callback = callback

    # ── Resume 기능 ──

    @staticmethod
    def _resume_path() -> Path:
        return Path.cwd() / ".ax-agent" / "resume.json"

    def _save_resume_state(self, state: dict, exit_reason: str) -> None:
        """중단 시 진행 상태를 .ax-agent/resume.json에 저장."""
        import json
        from langchain_core.messages import messages_to_dict

        path = self._resume_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        messages = state.get("messages", [])
        # 마지막 사용자 메시지 추출
        original_request = ""
        for msg in messages:
            if hasattr(msg, "type") and msg.type == "human":
                original_request = msg.content if isinstance(msg.content, str) else str(msg.content)
                break

        # AI가 지금까지 한 작업 요약 (마지막 AI 메시지)
        last_ai_content = ""
        for msg in reversed(messages):
            if hasattr(msg, "type") and msg.type == "ai" and hasattr(msg, "content") and msg.content:
                last_ai_content = msg.content if isinstance(msg.content, str) else str(msg.content)
                break

        resume_data = {
            "original_request": original_request,
            "progress_summary": last_ai_content[:2000],
            "iteration": state.get("iteration", 0),
            "exit_reason": exit_reason,
            "current_tier": state.get("current_tier") or get_config().orchestrator_tier,
            "project_id": state.get("project_id", ""),
        }

        path.write_text(json.dumps(resume_data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("resume_state.saved", path=str(path))

    def has_resume_state(self) -> bool:
        """이어서 할 작업이 있는지 확인."""
        return self._resume_path().exists()

    def get_resume_info(self) -> dict | None:
        """저장된 resume 정보를 반환."""
        import json
        path = self._resume_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    async def run_resume(self) -> dict[str, Any]:
        """중단된 작업을 이어서 실행.

        저장된 원본 요청 + 진행 상황을 새 프롬프트로 구성하여 실행.
        """
        import json
        path = self._resume_path()
        if not path.exists():
            return {"final_response": "이어서 할 작업이 없습니다.", "exit_reason": "no_resume"}

        resume = json.loads(path.read_text(encoding="utf-8"))

        # resume 파일 삭제 (한 번만 사용)
        path.unlink(missing_ok=True)

        # 이어서 할 프롬프트 구성
        resume_prompt = f"""이전 작업을 이어서 진행해주세요.

## 원본 요청
{resume['original_request']}

## 이전 진행 상황 ({resume['iteration']}번째 iteration에서 {resume['exit_reason']}로 중단)
{resume['progress_summary'][:1500]}

## 지시사항
위 원본 요청에서 아직 완료되지 않은 부분을 이어서 진행하세요.
이미 생성된 파일은 read_file/glob_files로 확인한 후, 누락된 부분만 작업하세요.
"""

        return await self.run(resume_prompt, project_id=resume.get("project_id"))

    def close(self) -> None:
        """리소스 정리."""
        self._store.close()
