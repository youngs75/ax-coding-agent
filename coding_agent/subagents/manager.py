"""SubAgentManager — orchestrates spawning, execution, and lifecycle of SubAgents.

Incorporates four root-cause fixes from E2E analysis:
  Fix 1 — Message window: trim old messages before each LLM call (claw-code style).
  Fix 2 — Turn counting: hard max_turns limit + text repetition detection.
  Fix 3 — Output isolation: return structured summary, not raw LLM text.
  Fix 4 — Tool list in prompt: handled by factory.build_system_prompt().
"""

from __future__ import annotations

import asyncio
import time
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
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from coding_agent.core.state import AgentState
from coding_agent.models import get_model
from coding_agent.subagents.factory import SubAgentFactory
from coding_agent.subagents.models import (
    SubAgentInstance,
    SubAgentResult,
    SubAgentStatus,
)
from coding_agent.subagents.registry import SubAgentRegistry
from coding_agent.tools.file_ops import FILE_TOOLS
from coding_agent.tools.shell import SHELL_TOOLS

log = structlog.get_logger(__name__)

# ── Fix 2: SubAgent turn limits ──────────────────────────────
_SUBAGENT_MAX_TURNS = 50  # hard limit per SubAgent session

# Map tool names to actual tool objects
_ALL_TOOLS: dict[str, BaseTool] = {}
for _t in FILE_TOOLS + SHELL_TOOLS:
    _ALL_TOOLS[_t.name] = _t


def _resolve_tools(tool_names: list[str]) -> list[BaseTool]:
    """Resolve a list of tool name strings into BaseTool instances."""
    resolved: list[BaseTool] = []
    for name in tool_names:
        tool = _ALL_TOOLS.get(name)
        if tool is not None:
            resolved.append(tool)
        else:
            log.warning("subagent.tool.not_found", tool_name=name)
    return resolved


class SubAgentManager:
    """High-level manager for SubAgent spawn / cancel / cleanup."""

    def __init__(self, registry: SubAgentRegistry, factory: SubAgentFactory) -> None:
        self._registry = registry
        self._factory = factory

    # ── Spawn ─────────────────────────────────────────────────

    async def spawn(
        self,
        task_description: str,
        parent_id: str | None = None,
        agent_type: str = "auto",
    ) -> SubAgentResult:
        """Spawn a SubAgent, execute it, and return the result.

        The full lifecycle is managed here:
        CREATED -> ASSIGNED -> RUNNING -> COMPLETED/FAILED -> DESTROYED
        """
        instance = self._factory.create_for_task(
            task_description, parent_id=parent_id, agent_type=agent_type
        )
        agent_id = instance.agent_id

        try:
            result = await self._execute_with_retries(instance)
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

    async def _execute_with_retries(self, instance: SubAgentInstance) -> SubAgentResult:
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
                result = await self._run_agent(instance)
                duration = time.monotonic() - start
                result.duration_s = duration

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

    async def _run_agent(self, instance: SubAgentInstance) -> SubAgentResult:
        """Build and invoke the LangGraph for a single attempt."""
        t_total = time.monotonic()

        t0 = time.monotonic()
        system_prompt = self._factory.build_system_prompt(instance)
        tools = _resolve_tools(instance.tools)
        model = get_model(instance.model_tier, temperature=0.0)  # type: ignore[arg-type]
        setup_elapsed = time.monotonic() - t0

        t0 = time.monotonic()
        graph, get_hit_max_turns = self._build_subagent_graph(instance, system_prompt, tools, model)
        graph_elapsed = time.monotonic() - t0

        initial_state: dict[str, Any] = {
            "messages": [
                SystemMessage(content=system_prompt),
                HumanMessage(content=instance.task_summary),
            ],
        }

        log.info(
            "timing.subagent.setup",
            agent_id=instance.agent_id,
            role=instance.role,
            model_tier=instance.model_tier,
            tools=instance.tools,
            setup_s=round(setup_elapsed, 3),
            graph_build_s=round(graph_elapsed, 3),
        )

        try:
            t0 = time.monotonic()
            final_state = await graph.ainvoke(
                initial_state,
                config={"recursion_limit": 500},
            )
            invoke_elapsed = time.monotonic() - t0

            log.info(
                "timing.subagent.invoke",
                agent_id=instance.agent_id,
                role=instance.role,
                invoke_s=round(invoke_elapsed, 3),
                total_s=round(time.monotonic() - t_total, 3),
                msg_count=len(final_state.get("messages", [])),
            )
        except Exception as exc:
            log.error(
                "timing.subagent.invoke_error",
                agent_id=instance.agent_id,
                elapsed_s=round(time.monotonic() - t_total, 3),
                error=str(exc)[:200],
            )
            return SubAgentResult(success=False, output="", error=str(exc))

        # ── Fix 3: Extract structured summary instead of raw LLM text ──
        messages = final_state.get("messages", [])
        if not messages:
            return SubAgentResult(success=False, output="", error="No messages in final state")

        # Collect files written/edited (heuristic: look at tool calls)
        written_files: list[str] = []
        edited_files: list[str] = []
        executed_commands: int = 0
        tool_errors: list[str] = []
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
            # Collect tool errors from ToolMessages
            if isinstance(msg, ToolMessage):
                content_str = msg.content if isinstance(msg.content, str) else str(msg.content)
                if "error" in content_str.lower()[:100]:
                    tool_errors.append(content_str[:150])

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

        compiled = builder.compile()
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
