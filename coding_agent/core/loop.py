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
from langgraph.types import Command

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
# NOT gated — ledger needs to register, planner may need re-delegation, and
# researcher/read-only exploration is safe.
_GATED_ROLES = frozenset({"coder", "verifier", "fixer", "reviewer"})


def _requires_decomposition_gate(
    last_message: Any,
    todo_counts: dict[str, int],
    confirmed: bool,
) -> tuple[bool, str | None]:
    """Decide whether to block before a non-ledger task delegation.

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


def _detect_implicit_decomposition_confirm(
    messages: list,
    todo_counts: dict[str, int],
) -> bool:
    """Infer that the user has already been consulted *about this ledger*.

    Only counts ``ask_user_question`` interactions that happened **after**
    the orchestrator's last ``task("ledger", ...)`` delegation. Pre-ledger
    asks (e.g. planner's early tech-stack questions) do *not* qualify —
    those are about scope/framework decisions, not decomposition
    granularity.

    Avoids the v3 noise where an orchestrator that proactively calls
    ``ask_user_question`` (per the prompt "분해 확인" section) then trips
    the gate on its follow-up ``task(coder)`` call, while also avoiding
    the v8 regression where pre-ledger asks falsely flipped the flag and
    the user never saw the granularity question.
    """
    if sum(todo_counts.values()) == 0:
        return False
    last_ledger_idx = -1
    for i, m in enumerate(messages):
        tool_calls = getattr(m, "tool_calls", None) or []
        for tc in tool_calls:
            if tc.get("name") != "task":
                continue
            args = tc.get("args") or {}
            agent_type = (args.get("agent_type") or "").strip().lower()
            if agent_type == "ledger":
                last_ledger_idx = i
                break
    if last_ledger_idx < 0:
        return False
    for m in messages[last_ledger_idx + 1:]:
        if isinstance(m, ToolMessage) and getattr(m, "name", "") == "ask_user_question":
            return True
    return False


def _build_gate_decomposition_message(
    counts: dict[str, int],
    preview: list[str],
) -> str:
    """Render the ToolMessage injected when a delegation is gated.

    ``preview`` is a list of "TASK-NN: <content>" lines the caller has already
    truncated. Kept pure so tests can snapshot layout.
    """
    n = counts.get("pending", 0) + counts.get("in_progress", 0) + counts.get("completed", 0)
    shown = preview[:5]
    preview_block = "\n".join(f"    - {line}" for line in shown)
    if n > len(shown):
        preview_block += f"\n    ... 외 {n - len(shown)}개"
    return (
        f"⚠ 분해 확인 필요: ledger 에 {n}개 task 가 등록되어 있는데, 사용자의 "
        f"granularity 승인 없이 위임하려 했습니다. harness 가 이 위임을 1회 "
        f"차단합니다.\n\n"
        f"현재 분해 미리보기:\n{preview_block}\n\n"
        f"지금 바로 `ask_user_question` 을 호출해서 다음을 묻고, 답변에 따라 "
        f"재분해(ledger clear + planner 재위임) 또는 그대로 진행(coder 위임)을 "
        f"결정하세요:\n"
        f"- 분해된 개수({n}개)가 적절한가?\n"
        f"- 더 세분화 / 더 통합 / 이대로 진행 중 어느 것을 원하는가?\n\n"
        f"사용자 답변을 받은 뒤 다시 task 도구로 위임하면 통과됩니다. "
        f"(게이트는 1회만 차단하지만, 사용자 확인 없이 위임을 재시도하는 것은 "
        f"금지입니다.)"
    )


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
진행 관리입니다 — 요구사항 분석·task 분해·산출물 설계는 planner 에게,
ledger 기록은 ledger 에게 위임하세요.

## 사용 가능한 도구
- read_file / glob_files / grep: 결과물 확인용
- task: SubAgent 위임 (코드 작성/수정/실행, ledger 기록 모두 이 경로)

## SubAgent 역할
- planner: 요구사항 분석, PRD/SPEC 등 기획 산출물 작성, task 분해 (분해 강도는 ledger 등록 후 사용자 확인 게이트가 결정)
- coder: 코드 작성·수정·실행
- verifier: 테스트/빌드 검증 (수정 금지)
- fixer: 지정된 실패 지점을 타겟팅해 수정
- reviewer: 코드 품질 검토
- researcher: 코드/문서 탐색
- ledger: planner 가 돌려준 분해 결과를 todo ledger 에 등록하거나, 특정
  task 의 상태를 수동으로 업데이트. 내용을 생성하지 않는 registrar.

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

## Todo ledger 운용
- ledger 기록은 orchestrator 가 직접 하지 않고 ledger SubAgent 에게 위임
  합니다. 초기 등록은 planner 가 돌려준 분해 결과를 ledger 에게 넘겨
  write_todos 로 등록하도록 하세요.
- 등록 순서 = 작업 순서. pending 첫 항목부터 진행하세요.
- task description 첫 줄에 `TASK-NN: ...` 을 포함하면 harness 가 자동으로
  in_progress/completed 마킹합니다. 수동 update 가 필요하면 ledger 에게
  "TASK-NN 을 <status> 로" 와 같은 task 로 위임하세요.
- verifier 가 보고한 실패(에러 메시지·테스트명·스택)를 fixer description 에
  그대로 복사하세요.

{decomposition_section}

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


# ── 분해 확인 (decomposition gate) — 동적 섹션 빌더 ───────────────────────
# v7 회귀(2026-04-26): SYSTEM_PROMPT 가 정적이라 ``decomposition_confirmed``
# 가 True 가 된 후에도 같은 "ledger 직후 ask 호출" 안내가 그대로 보임 →
# orchestrator(qwen3-max) 가 답을 받고도 같은 질문을 planner 에게 또
# 위임하는 무한 루프. 동적 섹션으로 confirmed 상태를 명시한다.

_DECOMPOSITION_PENDING_TEXT = """\
## 분해 확인 (중요 — coder/verifier/fixer/reviewer 위임 전 필수)
- ledger 가 초기 등록을 마친 직후, **반드시 사용자에게 분해 granularity 를
  확인받은 뒤** coder/verifier/fixer/reviewer 에게 위임하세요.
- 방법: `ask_user_question` 도구 (task 위임 없이 orchestrator 가 직접 호출
  가능) 로 다음을 묻습니다:
    1. 분해된 task 개수와 첫 3-5개 미리보기 제시
    2. "더 세분화 / 더 통합 / 이대로 진행" 중 선택 요청
- 사용자 응답에 따라:
    - "이대로 진행" → TASK-01 부터 coder/verifier 위임 시작
    - "더 세분화" / "더 통합" → ledger 에게 "모든 todos 를 clear" 위임 후,
      planner 에게 사용자 피드백과 목표 개수(예: "20-25개 수준으로 각 API
      endpoint 를 개별 task 로 분리")를 명시해 재위임
- harness 안전망: 이 단계를 건너뛰고 coder/verifier/fixer/reviewer 에게
  바로 위임하면 `task` 도구가 1회 차단되고 같은 안내를 받습니다. 차단
  직후에도 **반드시 `ask_user_question` 을 먼저 호출하세요** — 차단을
  단순히 "한 번 통과하면 지나감" 으로 취급하지 마십시오."""


_DECOMPOSITION_CONFIRMED_TEXT = """\
## 분해 확인 — 완료
ledger 등록 후 분해 granularity 에 대한 사용자 확인이 끝났습니다.
**같은 분해 확인 질문을 다시 호출하거나 planner 에게 'ask_user_question'
관련 task 를 위임하지 마세요** — 사용자가 이미 답했고 그 답은 위 "사용자
결정 사항" 에 기록돼 있습니다.

다음 단계:
- 사용자 답변이 "이대로 진행" 류라면 TASK-01 부터 순서대로 coder/verifier
  위임을 시작하세요.
- "더 세분화" 또는 "더 통합" 류라면 ledger 에게 "모든 todos 를 clear" 를
  위임한 뒤 planner 에게 사용자 피드백과 목표 개수를 명시해 재위임하세요."""


def _build_decomposition_section(confirmed: bool) -> str:
    return _DECOMPOSITION_CONFIRMED_TEXT if confirmed else _DECOMPOSITION_PENDING_TEXT


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

        # ── Fix 1: Orchestrator message window ────────────────
        _ORCH_MAX_MESSAGES = 60  # keep system + last N messages

        def _trim_orchestrator_messages(messages: list) -> list:
            """Trim orchestrator message history.

            Preserves:
              [0] SystemMessage (system prompt)
              [1] HumanMessage  (user request — MUST NOT be trimmed)
              [-N:] Most recent messages
            """
            if len(messages) <= _ORCH_MAX_MESSAGES + 2:
                return messages
            from langchain_core.messages import SystemMessage as _Sys
            # Keep system prompt + user's original request
            head = messages[:2]
            recent = messages[-_ORCH_MAX_MESSAGES:]
            log.info(
                "orchestrator.message_window.trimmed",
                before=len(messages),
                after=len(head) + len(recent),
            )
            return head + recent

        def agent_node(state: AgentState) -> dict[str, Any]:
            """LLM 호출 노드.

            오픈소스 모델 호환성:
            1. native tool calling 지원 → bind_tools 사용
            2. 미지원 (GLM, MiniMax 등) → 프롬프트에 도구 스키마 주입,
               텍스트 응답에서 tool_call JSON 블록 파싱
            3. 메시지 전처리: 고아 tool_call 정리, DashScope 직렬화 보장
            4. Fix 1: 메시지 윈도우 적용 (토큰 증가 방지)
            """
            t0 = time.monotonic()
            tier = state.get("current_tier") or get_config().orchestrator_tier
            iteration = (state.get("iteration") or 0) + 1
            model, use_prompt_tools = get_bound_model(tier)

            # Fix 1: Trim messages before LLM call
            messages = _trim_orchestrator_messages(list(state.get("messages", [])))

            # 시스템 프롬프트 구성
            memory_ctx = state.get("memory_context", "")
            ledger_snapshot = _render_ledger_snapshot(self._todo_store)
            decomposition_section = _build_decomposition_section(
                state.get("decomposition_confirmed", False)
            )
            user_decisions_block = _build_user_decisions_block(
                self._user_decisions.header()
            )
            sys_prompt = SYSTEM_PROMPT.format(
                memory_context=memory_ctx,
                ledger_snapshot=ledger_snapshot,
                decomposition_section=decomposition_section,
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

            # Implicit decomposition confirm — ledger 등록 후 ask_user_question
            # 이력이 있으면 게이트를 스킵하고 flag 를 미리 전환. 모범생 흐름에서
            # 불필요한 gate 차단을 제거한다 (v3 E2E 관찰).
            if not state.get("decomposition_confirmed", False):
                if _detect_implicit_decomposition_confirm(
                    messages, self._todo_store.counts()
                ):
                    log.info("orchestrator.decomposition_confirmed_implicit")
                    result["decomposition_confirmed"] = True

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
            """Block the first non-ledger delegation after ledger fill.

            Responds with a ``ToolMessage`` so LangChain does not raise on
            an orphan tool_call, and flips ``decomposition_confirmed`` to
            True so subsequent delegations pass. The system prompt instructs
            the orchestrator to call ``ask_user_question`` on receiving this
            block message — harness enforces the first gate, prompt enforces
            the follow-through.
            """
            counts = self._todo_store.counts()
            items = self._todo_store.list_items()
            preview = [f"{it.id}: {it.content[:80]}" for it in items[:5]]
            messages = state.get("messages", [])
            last_msg = messages[-1] if messages else None
            _, blocked_id = _requires_decomposition_gate(
                last_msg,
                counts,
                state.get("decomposition_confirmed", False),
            )
            if blocked_id is None:
                # Safety: route decided to gate but id went missing — just
                # clear the flag so we don't deadlock.
                return {"decomposition_confirmed": True}
            body = _build_gate_decomposition_message(counts, preview)
            log.warning(
                "orchestrator.decomposition_gate_blocked",
                pending=counts.get("pending", 0),
                in_progress=counts.get("in_progress", 0),
                completed=counts.get("completed", 0),
                total=sum(counts.values()),
                blocked_tool_call_id=blocked_id,
            )
            return {
                "messages": [ToolMessage(content=body, tool_call_id=blocked_id)],
                "decomposition_confirmed": True,
            }

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
    ) -> dict[str, Any]:
        """사용자 메시지를 처리하고 최종 상태를 반환한다.

        ``ask_user`` is an optional async callback used to satisfy
        ``ask_user_question`` interrupts. It receives the interrupt
        payload (a dict produced by the tool) and must return the
        user's answer (any JSON-serializable value). If omitted and
        an interrupt fires, the run aborts with exit_reason='no_ask_user_handler'.
        """
        self._progress_guard.reset()

        initial_state: dict[str, Any] = {
            "messages": [HumanMessage(content=user_message)],
            "project_id": project_id or get_config().project_id or "",
            "working_directory": get_config().project_root.as_posix(),
        }

        # Each user request gets its own thread so checkpointer state
        # does not leak between turns. The interrupt-resume loop below
        # uses the same thread_id to continue execution.
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
