"""SubAgentManager — orchestrates spawning, execution, and lifecycle of SubAgents.

Incorporates four root-cause fixes from E2E analysis:
  Fix 1 — Message window: trim old messages before each LLM call (claw-code style).
  Fix 2 — Turn counting: hard max_turns limit + text repetition detection.
  Fix 3 — Output isolation: return structured summary, not raw LLM text.
  Fix 4 — Tool list in prompt: handled by factory.build_system_prompt().
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from typing import Any, Sequence

import structlog
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

from coding_agent.core.state import AgentState
from coding_agent.models import get_model
from coding_agent.subagents.factory import SubAgentFactory
from coding_agent.subagents.models import (
    SubAgentInstance,
    SubAgentResult,
    SubAgentStatus,
)
from coding_agent.subagents.registry import SubAgentRegistry
from coding_agent.tools.ask_tool import build_ask_user_question_tool
from coding_agent.tools.file_ops import FILE_TOOLS
from coding_agent.tools.shell import SHELL_TOOLS
from coding_agent.tools.todo_tool import (
    TodoStore,
    build_update_todo_tool,
    build_write_todos_tool,
)

log = structlog.get_logger(__name__)

# ── Fix 2: SubAgent turn limits ──────────────────────────────
# Raised from 50 → 100 after E2E showed complex tasks (frontend forms,
# admin dashboards) legitimately needing more turns when using open-source
# models. 50 turns hit max for 1 out of 9 coders in the GLM5 run but the
# work was clearly valid, just unfinished.
_SUBAGENT_MAX_TURNS = 100  # hard limit per SubAgent session

# Map tool names to actual tool objects (static / shareable across sessions)
_ALL_TOOLS: dict[str, BaseTool] = {}
for _t in FILE_TOOLS + SHELL_TOOLS:
    _ALL_TOOLS[_t.name] = _t

# Reserved for future per-session tool builders that own state but do
# not need a manager callback. Currently empty — manager-scoped tools
# (ask_user_question, write_todos/update_todo) are wired directly inside
# SubAgentManager._resolve_tools because they need access to ``self``.
_STATIC_DYNAMIC_TOOL_BUILDERS: dict[str, callable] = {}  # type: ignore[type-arg]


class SubAgentManager:
    """High-level manager for SubAgent spawn / cancel / cleanup."""

    def __init__(self, registry: SubAgentRegistry, factory: SubAgentFactory) -> None:
        self._registry = registry
        self._factory = factory
        # ── Interrupt-aware spawn cache ──
        # When a SubAgent pauses on a LangGraph interrupt(), the caller
        # (task_tool) records the (graph, thread_id, instance) here so a
        # subsequent resume call can find the *same* compiled graph and
        # the *same* thread_id, avoiding a fresh spawn that would re-run
        # the LLM and rewrite files.  Keyed by a deterministic hash of
        # the spawn arguments — same description+agent_type → same key.
        self._paused_runs: dict[str, dict[str, Any]] = {}
        # ── User decisions accumulator ──
        # Every time a SubAgent's ask_user_question tool resolves, the
        # formatted answer is appended here. Subsequent SubAgent spawns
        # prepend this list to their task description so coder/verifier
        # /fixer all see the same hard constraints the planner asked for.
        self._user_decisions: list[str] = []
        # ── Todo ledger ──
        # Owned by the manager so the same store survives across multiple
        # orchestrator turns within one user session. The CLI may register
        # an ``on_change`` callback via set_todo_change_callback to render
        # the live ledger as a Rich Panel.
        self._todo_store = TodoStore()
        self._todo_change_callback = None  # type: ignore[var-annotated]

    # ── Decision accumulator API ─────────────────────────────
    def record_user_decision(self, formatted_answer: str) -> None:
        """Append a formatted ask_user_question answer to the session log."""
        if formatted_answer and formatted_answer not in self._user_decisions:
            self._user_decisions.append(formatted_answer)
            log.info(
                "subagent.user_decision_recorded",
                count=len(self._user_decisions),
                preview=formatted_answer[:80],
            )

    def get_user_decisions(self) -> list[str]:
        """Return accumulated user decisions (for logging/tests)."""
        return list(self._user_decisions)

    def _decisions_header(self) -> str:
        """Render accumulated decisions as a prepend block, or ''."""
        if not self._user_decisions:
            return ""
        lines = ["## 사용자 결정 사항 (하드 제약)"]
        for d in self._user_decisions:
            lines.append(f"- {d}")
        lines.append("")
        lines.append("위 결정 사항은 사용자가 직접 답변한 내용입니다. "
                     "이 제약을 벗어나는 기능/구성요소/스타일을 추가하지 마세요.")
        lines.append("")
        lines.append("## 작업 내용")
        return "\n".join(lines) + "\n"

    # ── Todo ledger API ──────────────────────────────────────
    def get_todo_store(self) -> TodoStore:
        """Return the manager-owned TodoStore (for tests / CLI inspection)."""
        return self._todo_store

    def set_todo_change_callback(self, callback) -> None:
        """Register a function called with the updated todo list after
        each successful write_todos / update_todo invocation.

        The callback must not raise — exceptions are swallowed by the tool.
        """
        self._todo_change_callback = callback

    def auto_advance_todo(self, task_id: str, status: str) -> bool:
        """Best-effort auto-update for a known todo id.

        Used by ``task_tool`` to flip a todo to ``in_progress`` when
        delegation starts and to ``completed`` when a coder finishes,
        without requiring the LLM to call ``update_todo`` explicitly.
        Silently no-ops when the id is not in the ledger so behavior
        degrades gracefully.

        Returns True when the store actually changed.
        """
        if not task_id:
            return False
        try:
            current = self._todo_store.list_items()
        except Exception:
            return False
        match = next((it for it in current if it.id == task_id), None)
        if match is None:
            return False
        if match.status == status:
            return False
        # Don't downgrade a completed todo back to in_progress
        if match.status == "completed" and status != "completed":
            return False
        try:
            self._todo_store.update(task_id, status)  # type: ignore[arg-type]
        except KeyError:
            return False
        if self._todo_change_callback is not None:
            try:
                self._todo_change_callback(self._todo_store.list_items())
            except Exception:
                pass
        log.info(
            "subagent.todo.auto_advance",
            task_id=task_id,
            status=status,
        )
        return True

    def build_todo_tools(self) -> list[BaseTool]:
        """Build a fresh pair of (write_todos, update_todo) tools bound
        to the manager's store and current change callback.

        The orchestrator builds these once at construction time.
        """
        return [
            build_write_todos_tool(
                store=self._todo_store,
                on_change=self._todo_change_callback,
            ),
            build_update_todo_tool(
                store=self._todo_store,
                on_change=self._todo_change_callback,
            ),
        ]

    def _resolve_tools(self, tool_names: list[str]) -> list[BaseTool]:
        """Resolve a list of tool name strings into BaseTool instances.

        Static tools come from the shared registry. Dynamic tools (those
        with per-session state) are built fresh on every call so each
        SubAgent invocation gets its own store. Manager-scoped tools
        (ask_user_question, write_todos/update_todo) are built with
        ``self`` captured in closure so they share the manager's stores.
        """
        resolved: list[BaseTool] = []
        for name in tool_names:
            builder = _STATIC_DYNAMIC_TOOL_BUILDERS.get(name)
            if builder is not None:
                resolved.append(builder())
                continue
            if name == "ask_user_question":
                resolved.append(
                    build_ask_user_question_tool(on_answer=self.record_user_decision)
                )
                continue
            if name == "write_todos":
                resolved.append(
                    build_write_todos_tool(
                        store=self._todo_store,
                        on_change=self._todo_change_callback,
                    )
                )
                continue
            if name == "update_todo":
                resolved.append(
                    build_update_todo_tool(
                        store=self._todo_store,
                        on_change=self._todo_change_callback,
                    )
                )
                continue
            tool = _ALL_TOOLS.get(name)
            if tool is not None:
                resolved.append(tool)
            else:
                log.warning("subagent.tool.not_found", tool_name=name)
        return resolved

    @staticmethod
    def _spawn_key(task_description: str, agent_type: str) -> str:
        """Deterministic key for caching paused SubAgent runs.

        Uses a SHA-1 of the (description, agent_type) tuple. The same
        spawn call from a re-executed orchestrator node maps to the same
        key, which is what lets us resume instead of re-spawn.
        """
        h = hashlib.sha1()
        h.update(task_description.encode("utf-8"))
        h.update(b"\x00")
        h.update(agent_type.encode("utf-8"))
        return h.hexdigest()[:16]

    # ── Spawn ─────────────────────────────────────────────────

    async def spawn(
        self,
        task_description: str,
        parent_id: str | None = None,
        agent_type: str = "auto",
        resume_value: Any = None,
    ) -> SubAgentResult:
        """Spawn a SubAgent, execute it, and return the result.

        Lifecycle: CREATED -> ASSIGNED -> RUNNING -> COMPLETED/FAILED -> DESTROYED

        If a previous call with the same (task_description, agent_type)
        is paused on an interrupt, this call resumes that run instead of
        starting a new one. ``resume_value`` is forwarded as the answer
        to the inner ``interrupt()``.
        """
        key = self._spawn_key(task_description, agent_type)
        paused = self._paused_runs.get(key)

        if paused is not None:
            instance: SubAgentInstance = paused["instance"]
            graph = paused["graph"]
            thread_id: str = paused["thread_id"]
            get_hit_max_turns = paused["get_hit_max_turns"]

            # Idempotent re-spawn: when the orchestrator re-executes its
            # tools node after an interrupt, _run_task calls spawn() again
            # without an answer. We must NOT re-invoke the SubAgent in
            # that case — return the cached interrupt payload so the
            # task_tool's interrupt() picks up the user's stored answer.
            if resume_value is None:
                cached_payload = paused.get("interrupt_payload")
                log.info(
                    "subagent.resume_idempotent",
                    agent_id=instance.agent_id,
                    thread_id=thread_id,
                )
                return SubAgentResult(
                    success=True,
                    output="(awaiting user input)",
                    interrupt_payload=cached_payload,
                    thread_id=thread_id,
                )

            # Real resume: caller has the user's answer.
            log.info(
                "subagent.resume",
                agent_id=instance.agent_id,
                thread_id=thread_id,
                resume_preview=str(resume_value)[:60],
            )
            try:
                result = await self._invoke_graph(
                    instance=instance,
                    graph=graph,
                    thread_id=thread_id,
                    get_hit_max_turns=get_hit_max_turns,
                    initial_or_resume=Command(resume=resume_value),
                    spawn_key=key,
                )
                return result
            finally:
                # If the result is final (no more interrupts), drop the cache.
                if key in self._paused_runs and not self._paused_runs[key].get("paused"):
                    self._paused_runs.pop(key, None)
                    self._try_destroy(instance.agent_id, reason="resume_complete")

        # Fresh spawn path
        instance = self._factory.create_for_task(
            task_description, parent_id=parent_id, agent_type=agent_type
        )
        agent_id = instance.agent_id

        try:
            result = await self._execute_with_retries(instance, spawn_key=key)
            return result
        except Exception as exc:
            log.error(
                "subagent.spawn.unexpected_error",
                agent_id=agent_id,
                error=str(exc),
            )
            # Make sure we're in a destroyable state
            if instance.state == SubAgentStatus.RUNNING:
                self._registry.transition_state(
                    agent_id, SubAgentStatus.FAILED, reason=f"unexpected: {exc}"
                )
            instance.error = str(exc)
            return SubAgentResult(success=False, output="", error=str(exc))
        finally:
            # Always attempt cleanup to DESTROYED
            self._try_destroy(agent_id, reason="spawn_complete")

    async def _execute_with_retries(
        self,
        instance: SubAgentInstance,
        spawn_key: str | None = None,
    ) -> SubAgentResult:
        """Run the agent loop, retrying on failure up to max_retries."""
        agent_id = instance.agent_id

        while True:
            # CREATED/FAILED -> ASSIGNED
            if not self._registry.transition_state(
                agent_id, SubAgentStatus.ASSIGNED, reason="preparing"
            ):
                return SubAgentResult(
                    success=False,
                    output="",
                    error=f"Cannot assign agent {agent_id} (state={instance.state.value})",
                )

            # ASSIGNED -> RUNNING
            if not self._registry.transition_state(
                agent_id, SubAgentStatus.RUNNING, reason="starting"
            ):
                return SubAgentResult(
                    success=False,
                    output="",
                    error=f"Cannot start agent {agent_id} (state={instance.state.value})",
                )

            start = time.monotonic()
            try:
                result = await self._run_agent(instance, spawn_key=spawn_key)
                duration = time.monotonic() - start
                result.duration_s = duration

                # An interrupt is *not* a failure — return immediately so
                # the caller can propagate it. Lifecycle stays at RUNNING
                # until the resume completes (or fails).
                if result.interrupt_payload is not None:
                    return result

                if result.success:
                    self._registry.transition_state(
                        agent_id, SubAgentStatus.COMPLETED, reason="success"
                    )
                    instance.result = result.output
                    return result

                # Execution failed
                self._registry.transition_state(
                    agent_id, SubAgentStatus.FAILED, reason=result.error or "execution_failed"
                )
                instance.error = result.error

                # Retry?
                if instance.retry_count < instance.max_retries:
                    instance.retry_count += 1
                    log.info(
                        "subagent.retry",
                        agent_id=agent_id,
                        attempt=instance.retry_count,
                        max_retries=instance.max_retries,
                    )
                    continue  # loop back to ASSIGNED
                else:
                    log.warning(
                        "subagent.max_retries",
                        agent_id=agent_id,
                        retries=instance.retry_count,
                    )
                    return result

            except asyncio.TimeoutError:
                duration = time.monotonic() - start
                self._registry.transition_state(
                    agent_id, SubAgentStatus.FAILED, reason="timeout"
                )
                instance.error = "timeout"

                if instance.retry_count < instance.max_retries:
                    instance.retry_count += 1
                    log.info(
                        "subagent.timeout_retry",
                        agent_id=agent_id,
                        attempt=instance.retry_count,
                    )
                    continue
                return SubAgentResult(
                    success=False,
                    output="",
                    error="Agent timed out",
                    duration_s=duration,
                )

            except Exception as exc:
                duration = time.monotonic() - start
                self._registry.transition_state(
                    agent_id, SubAgentStatus.FAILED, reason=str(exc)
                )
                instance.error = str(exc)

                if instance.retry_count < instance.max_retries:
                    instance.retry_count += 1
                    log.info(
                        "subagent.error_retry",
                        agent_id=agent_id,
                        attempt=instance.retry_count,
                        error=str(exc),
                    )
                    continue
                return SubAgentResult(
                    success=False,
                    output="",
                    error=str(exc),
                    duration_s=duration,
                )

    async def _run_agent(
        self,
        instance: SubAgentInstance,
        spawn_key: str | None = None,
    ) -> SubAgentResult:
        """Build and invoke the LangGraph for a single attempt."""
        t_total = time.monotonic()

        t0 = time.monotonic()
        system_prompt = self._factory.build_system_prompt(instance)
        tools = self._resolve_tools(instance.tools)
        model = get_model(instance.model_tier, temperature=0.0)  # type: ignore[arg-type]
        setup_elapsed = time.monotonic() - t0

        t0 = time.monotonic()
        graph, get_hit_max_turns = self._build_subagent_graph(instance, system_prompt, tools, model)
        graph_elapsed = time.monotonic() - t0

        # Prepend accumulated user decisions to the human message so every
        # SubAgent sees the same hard constraints the planner collected.
        decisions_header = self._decisions_header()
        human_content = (
            decisions_header + instance.task_summary
            if decisions_header
            else instance.task_summary
        )
        initial_state: dict[str, Any] = {
            "messages": [
                SystemMessage(content=system_prompt),
                HumanMessage(content=human_content),
            ],
        }

        thread_id = f"sub-{instance.agent_id}-{uuid.uuid4().hex[:8]}"

        log.info(
            "timing.subagent.setup",
            agent_id=instance.agent_id,
            role=instance.role,
            model_tier=instance.model_tier,
            tools=instance.tools,
            setup_s=round(setup_elapsed, 3),
            graph_build_s=round(graph_elapsed, 3),
            thread_id=thread_id,
        )

        return await self._invoke_graph(
            instance=instance,
            graph=graph,
            thread_id=thread_id,
            get_hit_max_turns=get_hit_max_turns,
            initial_or_resume=initial_state,
            spawn_key=spawn_key,
            t_total=t_total,
        )

    async def _invoke_graph(
        self,
        instance: SubAgentInstance,
        graph: Any,
        thread_id: str,
        get_hit_max_turns: Any,
        initial_or_resume: Any,
        spawn_key: str | None = None,
        t_total: float | None = None,
    ) -> SubAgentResult:
        """Single ``ainvoke`` call against an already-built SubAgent graph.

        Used for both the fresh path (initial state) and the resume path
        (Command). Detects ``__interrupt__`` and returns a SubAgentResult
        with ``interrupt_payload`` set, plus caches the run in
        ``_paused_runs`` so a future ``spawn`` with the same ``spawn_key``
        will resume rather than re-execute.
        """
        if t_total is None:
            t_total = time.monotonic()

        config = {
            "recursion_limit": 500,
            "configurable": {"thread_id": thread_id},
        }

        try:
            t0 = time.monotonic()
            final_state = await graph.ainvoke(initial_or_resume, config=config)
            invoke_elapsed = time.monotonic() - t0

            log.info(
                "timing.subagent.invoke",
                agent_id=instance.agent_id,
                role=instance.role,
                invoke_s=round(invoke_elapsed, 3),
                total_s=round(time.monotonic() - t_total, 3),
                msg_count=len(final_state.get("messages", []) if isinstance(final_state, dict) else []),
            )
        except Exception as exc:
            log.error(
                "timing.subagent.invoke_error",
                agent_id=instance.agent_id,
                elapsed_s=round(time.monotonic() - t_total, 3),
                error=str(exc)[:200],
            )
            return SubAgentResult(success=False, output="", error=str(exc))

        # ── Interrupt path: ask_user_question (or any other) paused ──
        if isinstance(final_state, dict) and final_state.get("__interrupt__"):
            interrupts = final_state["__interrupt__"]
            first = interrupts[0] if isinstance(interrupts, (list, tuple)) else interrupts
            payload = getattr(first, "value", first)
            log.info(
                "subagent.paused_on_interrupt",
                agent_id=instance.agent_id,
                thread_id=thread_id,
            )
            if spawn_key is not None:
                self._paused_runs[spawn_key] = {
                    "instance": instance,
                    "graph": graph,
                    "thread_id": thread_id,
                    "get_hit_max_turns": get_hit_max_turns,
                    "paused": True,
                    "interrupt_payload": payload,
                }
            return SubAgentResult(
                success=True,
                output="(awaiting user input)",
                interrupt_payload=payload,
                thread_id=thread_id,
            )

        # ── Resume completed: drop the cache entry if any ──
        if spawn_key is not None and spawn_key in self._paused_runs:
            self._paused_runs[spawn_key]["paused"] = False

        # ── Fix 3: Extract structured summary instead of raw LLM text ──
        messages = final_state.get("messages", [])
        if not messages:
            return SubAgentResult(success=False, output="", error="No messages in final state")

        is_verifier = instance.role == "verifier"

        # Collect files written/edited (heuristic: look at tool calls)
        written_files: list[str] = []
        edited_files: list[str] = []
        executed_commands: int = 0
        tool_errors: list[str] = []
        # A-1: For verifier we additionally capture the *raw* execute tool
        # results (paired with their commands) so the orchestrator gets
        # exit codes / stdout tails instead of "Commands executed: 3".
        verifier_runs: list[dict[str, str]] = []
        pending_execute_cmd: str | None = None
        for msg in messages:
            if hasattr(msg, "tool_calls"):
                for tc in msg.tool_calls:
                    name = tc.get("name", "")
                    args = tc.get("args", {})
                    if name == "write_file":
                        path = args.get("path", "")
                        if path:
                            written_files.append(path)
                    elif name == "edit_file":
                        path = args.get("path", "")
                        if path:
                            edited_files.append(path)
                    elif name == "execute":
                        executed_commands += 1
                        if is_verifier:
                            pending_execute_cmd = (
                                args.get("command", "") if isinstance(args, dict) else ""
                            )
            # Collect tool errors from ToolMessages
            if isinstance(msg, ToolMessage):
                content_str = msg.content if isinstance(msg.content, str) else str(msg.content)
                if "error" in content_str.lower()[:100]:
                    tool_errors.append(content_str[:150])
                if is_verifier and pending_execute_cmd is not None:
                    # Pair the latest execute call with this tool result.
                    verifier_runs.append(
                        {
                            "command": pending_execute_cmd[:200],
                            "result": content_str,
                        }
                    )
                    pending_execute_cmd = None

        # Get the last AI message for a brief summary
        last_ai_content = ""
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and hasattr(msg, "content") and msg.content:
                raw = msg.content if isinstance(msg.content, str) else str(msg.content)
                last_ai_content = raw[:500]
                break

        # Build structured output (DeepAgents pattern: final message only)
        summary_parts = []
        if written_files:
            summary_parts.append(f"Files created: {', '.join(written_files)}")
        if edited_files:
            summary_parts.append(f"Files edited: {', '.join(edited_files)}")
        if executed_commands:
            summary_parts.append(f"Commands executed: {executed_commands}")
        if tool_errors:
            summary_parts.append(f"Errors encountered: {len(tool_errors)}")
        # A-1: Verifier-only — embed each execute(command, result) so the
        # orchestrator sees concrete exit codes / stdout tails. Without
        # this it loops verifier→fixer→verifier on "Commands executed: N"
        # because it has no failing test name to copy into the fixer's
        # description, the very thing the orchestrator prompt requires.
        if is_verifier and verifier_runs:
            run_blocks: list[str] = ["", "## Verifier execution results"]
            for i, run in enumerate(verifier_runs[-5:], 1):  # last 5 runs
                cmd = run["command"]
                res = run["result"]
                # Keep the head (often exit code / first error line) and
                # the tail (last assertion / traceback). Models tend to
                # truncate the middle anyway.
                head = res[:400]
                tail = res[-1000:] if len(res) > 1400 else ""
                body = head + ("\n... (truncated) ...\n" + tail if tail else "")
                run_blocks.append(f"### Run {i}: `{cmd}`")
                run_blocks.append(body.strip())
            summary_parts.append("\n".join(run_blocks))
        # Include a brief excerpt of the last AI response for context
        if last_ai_content:
            summary_parts.append(f"Summary: {last_ai_content}")

        structured_output = "\n".join(summary_parts) if summary_parts else "Task completed."

        # If max_turns was hit, mark as incomplete so Orchestrator knows
        if get_hit_max_turns():
            structured_output += (
                f"\n[INCOMPLETE — stopped at {_SUBAGENT_MAX_TURNS} turns. "
                "Some work may remain unfinished. Review files and continue if needed.]"
            )

        return SubAgentResult(
            success=True,
            output=structured_output,
            written_files=written_files,
        )

    # ── Graph builder ─────────────────────────────────────────

    @staticmethod
    def _build_subagent_graph(
        instance: SubAgentInstance,
        system_prompt: str,
        tools: Sequence[BaseTool],
        model: ChatOpenAI,
    ):
        """Build a simple ReAct-style LangGraph for a SubAgent.

        Flow: agent (LLM call) <-> tools, with early termination detection.

        Root-cause fixes applied:
          Fix 1 — Message window: trim to _SUBAGENT_MAX_MESSAGES before LLM call.
          Fix 2 — Turn counting (max_turns) + text repetition detection.
        """
        model_with_tools = model.bind_tools(tools) if tools else model

        # ── Fix 2: Turn & repetition tracking ─────────────────
        _recent_calls: list[tuple[str, str]] = []
        _MAX_REPEAT = 3
        _turn_count = 0
        _hit_max_turns = False  # signals incomplete work to caller
        _recent_texts: list[str] = []  # track consecutive text-only outputs
        _MAX_TEXT_REPEAT = 3  # stop after 3 identical text outputs

        # Collect valid tool names for error feedback (Fix 4)
        _valid_tool_names = {t.name for t in tools} if tools else set()

        def agent_node(state: dict[str, Any]) -> dict[str, Any]:
            """Call the LLM with full message history.

            No message trimming is applied here.  SubAgent context stays
            well within the model's 128K window (typically <15K tokens).
            The 60K token bloat problem was at the Orchestrator level where
            SubAgent results accumulated — NOT inside SubAgents themselves.
            """
            nonlocal _turn_count
            _turn_count += 1
            messages = state["messages"]
            response = model_with_tools.invoke(messages)
            return {"messages": [response]}

        def should_continue(state: dict[str, Any]) -> str:
            """Decide whether to call tools or finish, with multi-layer protection.

            Checks (in order):
            1. max_turns hard limit (Fix 2 — Claude Code pattern)
            2. No tool calls → check for text repetition (Fix 2)
            3. Invalid tool name → inject corrective feedback (Fix 4)
            4. Repeated identical tool calls → early stop
            """
            nonlocal _turn_count
            messages = state["messages"]
            last = messages[-1]

            # ── Fix 2: Hard turn limit (Claude Code maxTurns pattern) ──
            nonlocal _hit_max_turns
            if _turn_count >= _SUBAGENT_MAX_TURNS:
                _hit_max_turns = True
                log.warning(
                    "subagent.max_turns_reached",
                    agent_id=instance.agent_id,
                    turns=_turn_count,
                    max_turns=_SUBAGENT_MAX_TURNS,
                )
                return END

            # ── No tool calls: check text repetition ──
            if not (hasattr(last, "tool_calls") and last.tool_calls):
                # Fix 2: Detect repeated text-only outputs
                content = getattr(last, "content", "")
                if isinstance(content, str) and content.strip():
                    text_sig = content.strip()[:200]
                    _recent_texts.append(text_sig)
                    # Keep only last entries
                    while len(_recent_texts) > _MAX_TEXT_REPEAT * 2:
                        _recent_texts.pop(0)
                    if len(_recent_texts) >= _MAX_TEXT_REPEAT:
                        tail = _recent_texts[-_MAX_TEXT_REPEAT:]
                        if all(t == tail[0] for t in tail):
                            log.warning(
                                "subagent.early_stop.repeated_text",
                                agent_id=instance.agent_id,
                                text_preview=tail[0][:80],
                            )
                            return END
                return END

            # ── Fix 4: Check for invalid tool names ──
            if _valid_tool_names:
                for tc in last.tool_calls:
                    name = tc.get("name", "")
                    if name and name not in _valid_tool_names:
                        log.warning(
                            "subagent.invalid_tool",
                            agent_id=instance.agent_id,
                            tool=name,
                            valid=list(_valid_tool_names),
                        )
                        # Don't route to tools — will fail. Return END to
                        # let LangGraph's ToolNode handle the error naturally,
                        # but we still route to "tools" so the error feedback
                        # reaches the LLM for self-correction.

            # ── Detect repeated identical tool calls → likely stuck ──
            for tc in last.tool_calls:
                call_sig = (tc.get("name", ""), str(tc.get("args", {})))
                _recent_calls.append(call_sig)

            while len(_recent_calls) > _MAX_REPEAT * 2:
                _recent_calls.pop(0)

            if len(_recent_calls) >= _MAX_REPEAT:
                tail = _recent_calls[-_MAX_REPEAT:]
                if all(c == tail[0] for c in tail):
                    log.warning(
                        "subagent.early_stop.repeated_calls",
                        agent_id=instance.agent_id,
                        call=tail[0][0],
                    )
                    return END

            return "tools"

        # Build the graph
        builder = StateGraph(AgentState)
        builder.add_node("agent", agent_node)

        if tools:
            tool_node = ToolNode(tools)
            builder.add_node("tools", tool_node)
            builder.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
            builder.add_edge("tools", "agent")
        else:
            builder.add_edge("agent", END)

        builder.set_entry_point("agent")

        # InMemorySaver is required for interrupt() to work inside the
        # SubAgent (e.g. ask_user_question called from a planner). Each
        # SubAgent run gets its own checkpointer instance — runs do not
        # share state, and the cache in _paused_runs holds the live
        # graph reference until the resume completes.
        compiled = builder.compile(checkpointer=InMemorySaver())
        # Return both the graph and an accessor for the max_turns flag
        # so _run_agent() can report incomplete work to the Orchestrator.
        return compiled, lambda: _hit_max_turns

    # ── Parallel spawn ───────────────────────────────────────

    async def spawn_parallel(
        self,
        tasks: list[dict[str, str]],
    ) -> list[SubAgentResult]:
        """Spawn multiple independent SubAgents concurrently.

        Each item in *tasks* should have 'description' and optionally 'agent_type'.
        Returns results in the same order as input tasks.
        """
        coros = [
            self.spawn(
                task_description=t["description"],
                agent_type=t.get("agent_type", "auto"),
            )
            for t in tasks
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)

        final: list[SubAgentResult] = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                log.error(
                    "subagent.parallel.task_error",
                    task_index=i,
                    error=str(r),
                )
                final.append(SubAgentResult(success=False, output="", error=str(r)))
            else:
                final.append(r)
        return final

    # ── Cancel ────────────────────────────────────────────────

    async def cancel(self, agent_id: str) -> bool:
        """Cancel a running or assigned SubAgent."""
        instance = self._registry.get_instance(agent_id)
        if instance is None:
            log.warning("subagent.cancel.not_found", agent_id=agent_id)
            return False

        ok = self._registry.transition_state(
            agent_id, SubAgentStatus.CANCELLED, reason="user_cancel"
        )
        if ok:
            log.info("subagent.cancelled", agent_id=agent_id)
        return ok

    # ── Cleanup ───────────────────────────────────────────────

    def cleanup(self) -> int:
        """Destroy old completed/failed instances. Returns count destroyed."""
        return self._registry.cleanup_completed()

    # ── Internal helpers ──────────────────────────────────────

    def _try_destroy(self, agent_id: str, reason: str = "cleanup") -> None:
        """Best-effort transition to DESTROYED. Silently handles failures."""
        instance = self._registry.get_instance(agent_id)
        if instance is None:
            return
        if instance.state == SubAgentStatus.DESTROYED:
            return
        # Some states need an intermediate transition before DESTROYED
        if instance.state == SubAgentStatus.RUNNING:
            self._registry.transition_state(
                agent_id, SubAgentStatus.FAILED, reason="force_cleanup"
            )
        self._registry.destroy_instance(agent_id, reason=reason)
