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
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

from coding_agent.config import get_config
from coding_agent.core.orchestrator import should_delegate
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
from coding_agent.tools.file_ops import FILE_TOOLS
from coding_agent.tools.shell import SHELL_TOOLS
from coding_agent.tools.task_tool import build_task_tool
from coding_agent.tools.todo_tool import (
    TodoStore,
    build_update_todo_tool,
    build_write_todos_tool,
)

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

SYSTEM_PROMPT = """당신은 Orchestrator AI Coding Agent입니다.
직접 코드를 작성하지 않고, task 도구로 전문 SubAgent에게 위임합니다.

## 사용 가능한 도구
- read_file / glob_files / grep: 결과물 확인용
- task: SubAgent 위임 (코드 작성/수정/실행은 모두 이 경로)
- write_todos / update_todo: 진행 상황 ledger

## SubAgent 역할
- planner: 요구사항 분석, PRD/SPEC 작성
- coder: 코드 생성, 파일 작성, 패키지 설치, 테스트 작성
- verifier: 테스트 실행, 빌드 확인 (수정 없음)
- fixer: 버그 수정, 에러 해결
- reviewer: 코드 품질 검토
- researcher: 탐색/조사

## 작업 흐름 (복잡한 개발 요청)
1. **PRD 위임** — task(planner, "PRD를 작성하세요. ...") 단독 호출. 사용자가 지정한
   요구사항·구조·범위를 그대로 description에 전달합니다. PRD와 다른 산출물을
   같은 호출에 섞지 마세요.
2. **SPEC 위임** — PRD 완료 후 별도 task(planner, "SPEC/SDD를 작성하세요. docs/PRD.md를
   먼저 읽고 작성"). 사용자가 SPEC 구조(섹션 목록, Phase 분할 등)를 명시했다면
   그 구조를 description에 그대로 전달하세요. harness는 SPEC 형식을 강제하지
   않으므로 planner가 자율적으로 작성합니다.
3. **Todo 등록** — SPEC을 read_file로 읽은 직후 **반드시 write_todos를 한 번 호출**해
   SPEC의 모든 atomic task를 **SPEC에 등장한 순서 그대로** ledger에 등록합니다.
   todo id는 SPEC에서 사용된 task 식별자(TASK-01 등)를 그대로 사용합니다.
   임의로 순서를 바꾸거나 일부만 등록하지 마세요.
4. **구현 루프** — todo가 남아 있는 동안:
   - **항상 pending 중에서 가장 첫 번째 todo부터 진행**합니다 (등록 순서 = 의존성 순서).
   - 위임 시 task description의 첫 줄에 반드시 `TASK-NN: ...` 형식을 포함하면
     harness가 자동으로 해당 todo를 in_progress로 마킹합니다 (update_todo 호출 불필요).
   - coder가 성공적으로 마무리하면 harness가 자동으로 todo를 completed로 마킹합니다.
     verifier/fixer 사이클은 todo를 in_progress로 유지합니다.
   - update_todo는 자동 진행에서 누락된 경우(예: planner/researcher 위임)에만
     수동으로 호출하면 됩니다.
   - **pending과 in_progress가 모두 0이 될 때까지 멈추지 말고 다음 todo로 진행**합니다.
     자연어로 "이제 SPEC을 확인하겠습니다" 같은 응답만 하고 멈추면 안 됩니다 —
     항상 도구 호출(write_todos / task)로 다음 단계를 시작하세요.
   - 같은 TASK-NN을 여러 번 verifier↔fixer로 반복하지 마세요. **harness가 동일
     TASK-NN을 6회 이상 재위임하면 ProgressGuard가 강제 종료합니다**. fixer가
     실패한 verifier 결과를 받았다면 verifier 출력의 구체적 에러(exit code,
     stack trace, 실패 테스트명)를 fixer description에 그대로 복사해 1회 안에
     해결되도록 하세요.
5. **검증** — 코드 변경이 있을 때마다 **반드시 verifier를 먼저 호출**합니다.
   fixer를 직접 부르지 마세요. verifier가 실패를 보고하면, 그 다음에만 fixer를
   호출하되 **verifier가 보고한 구체적 실패 내용(에러 메시지/실패 테스트명/스택 트레이스)을
   fixer의 task description에 반드시 복사해서 넣으세요**. fixer는 테스트를 직접
   돌리지 못하고 코드만 수정합니다 — 수정 후 다시 verifier를 호출해 통과를 확인합니다.
   전체 구현이 끝나면 reviewer를 한 번 호출해 품질을 점검합니다.

## 원칙
- 사용자가 요청하지 않은 기능을 임의로 추가하지 마세요.
- 모호한 요구사항(기술 스택/플랫폼/인증 범위/저장소 등)은 planner가 ask_user_question
  으로 먼저 확인합니다. harness는 산출물 형식이나 섹션 구조를 강제하지 않습니다 —
  사용자 입력에 충실히 따르되, 빠진 부분만 질문으로 채웁니다.

{memory_context}
"""


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
        self._orchestrator = build_orchestrator(
            memory_store=self._store,
            user_decisions=self._user_decisions,
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

        # Orchestrator 에는 읽기 전용 도구 + task 위임 도구 + todo ledger 만 바인딩.
        # write_file/edit_file/execute 는 SubAgent 전용 — orchestrator LLM 이 직접
        # 코드를 쓰면 23회 이상 직접 도구 호출이 발생하는 이슈가 E2E 에서 관찰됐음.
        from coding_agent.tools.file_ops import read_file, glob_files, grep
        todo_tools = [
            build_write_todos_tool(
                store=self._todo_store,
                on_change=lambda items: (
                    self._todo_change_callback(items)
                    if self._todo_change_callback
                    else None
                ),
            ),
            build_update_todo_tool(
                store=self._todo_store,
                on_change=lambda items: (
                    self._todo_change_callback(items)
                    if self._todo_change_callback
                    else None
                ),
            ),
        ]
        self._tools = [read_file, glob_files, grep, task_tool, *todo_tools]

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
                updates["iteration"] = 0
                updates["max_iterations"] = get_config().max_iterations
                updates["current_tier"] = "strong"
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
            tier = state.get("current_tier", "strong")
            iteration = (state.get("iteration") or 0) + 1
            model, use_prompt_tools = get_bound_model(tier)

            # Fix 1: Trim messages before LLM call
            messages = _trim_orchestrator_messages(list(state.get("messages", [])))

            # 시스템 프롬프트 구성
            memory_ctx = state.get("memory_context", "")
            sys_prompt = SYSTEM_PROMPT.format(memory_context=memory_ctx)

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

            if verdict == GuardVerdict.STOP:
                return {
                    "exit_reason": "progress_guard_stop",
                    "stall_count": (state.get("stall_count") or 0) + 1,
                }
            elif verdict == GuardVerdict.WARN:
                log.warning("progress_guard.stall_detected", iteration=iteration)
                return {"stall_count": (state.get("stall_count") or 0) + 1}

            return {}

        _consecutive_errors = 0
        _MAX_CONSECUTIVE_ERRORS = 3

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
                    return "tools"

            # 도구 호출 없음 = 응답 완료
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
                "extract_memory_final": "extract_memory_final",
                "handle_error": "handle_error",
                "safe_stop": "safe_stop",
            },
        )

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
            "current_tier": state.get("current_tier", "strong"),
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
