"""SubAgentManager — orchestrates spawning, execution, and lifecycle of SubAgents."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Sequence

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
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
        system_prompt = self._factory.build_system_prompt(instance)
        tools = _resolve_tools(instance.tools)
        model = get_model(instance.model_tier, temperature=0.0)  # type: ignore[arg-type]

        graph = self._build_subagent_graph(instance, system_prompt, tools, model)

        initial_state: dict[str, Any] = {
            "messages": [
                SystemMessage(content=system_prompt),
                HumanMessage(content=instance.task_summary),
            ],
        }

        try:
            final_state = await graph.ainvoke(
                initial_state,
                config={"recursion_limit": 500},
            )
        except Exception as exc:
            return SubAgentResult(success=False, output="", error=str(exc))

        # Extract the final assistant message
        messages = final_state.get("messages", [])
        if not messages:
            return SubAgentResult(success=False, output="", error="No messages in final state")

        last_msg = messages[-1]
        content = getattr(last_msg, "content", str(last_msg))

        # Collect any files that were written (heuristic: look at tool calls)
        written_files: list[str] = []
        for msg in messages:
            if hasattr(msg, "tool_calls"):
                for tc in msg.tool_calls:
                    if tc.get("name") == "write_file":
                        args = tc.get("args", {})
                        path = args.get("path", "")
                        if path:
                            written_files.append(path)

        return SubAgentResult(
            success=True,
            output=content if isinstance(content, str) else str(content),
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

        Flow: agent (LLM call) <-> tools, with a recursion limit.
        """
        model_with_tools = model.bind_tools(tools) if tools else model

        def agent_node(state: dict[str, Any]) -> dict[str, Any]:
            """Call the LLM with the current message history."""
            messages = state["messages"]
            response = model_with_tools.invoke(messages)
            return {"messages": [response]}

        def should_continue(state: dict[str, Any]) -> str:
            """Decide whether to call tools or finish."""
            messages = state["messages"]
            last = messages[-1]
            if hasattr(last, "tool_calls") and last.tool_calls:
                return "tools"
            return END

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

        return builder.compile(
            # recursion_limit controls max number of supersteps
        )

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
