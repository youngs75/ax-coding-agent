"""Task tool — allows the main agent to delegate work to SubAgents.

Exposes a LangChain StructuredTool named ``task`` that spawns a SubAgent
via SubAgentManager and returns the result.
"""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from coding_agent.subagents.manager import SubAgentManager


class TaskToolInput(BaseModel):
    """Input schema for the ``task`` tool."""

    description: str = Field(
        description="A detailed description of the task to delegate to a SubAgent."
    )
    agent_type: str = Field(
        default="auto",
        description=(
            "The type of SubAgent to spawn. "
            "Options: 'planner', 'coder', 'reviewer', 'fixer', 'researcher', 'auto'. "
            "Use 'auto' to let the system choose."
        ),
    )


def _run_task(
    manager: SubAgentManager,
    description: str,
    agent_type: str = "auto",
) -> str:
    """Synchronous wrapper that spawns a SubAgent and returns its output."""
    try:
        # Get or create an event loop to run the async spawn
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            # We're inside an already-running event loop (e.g., LangGraph async).
            # Use asyncio.ensure_future + a new thread to avoid deadlock.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, manager.spawn(description, agent_type=agent_type))
                result = future.result(timeout=300)
        else:
            result = asyncio.run(manager.spawn(description, agent_type=agent_type))

        if result.success:
            parts = [result.output]
            if result.written_files:
                parts.append(f"\n\nFiles written: {', '.join(result.written_files)}")
            parts.append(f"\n[Duration: {result.duration_s:.1f}s]")
            return "".join(parts)
        else:
            return f"SubAgent failed: {result.error or 'unknown error'}"

    except Exception as exc:
        return f"Error spawning SubAgent: {exc}"


def build_task_tool(manager: SubAgentManager) -> StructuredTool:
    """Build and return a LangChain StructuredTool that delegates tasks to SubAgents.

    The returned tool captures *manager* via closure so it can spawn SubAgents.
    """
    func = functools.partial(_run_task, manager)

    return StructuredTool.from_function(
        func=func,
        name="task",
        description=(
            "Delegate a task to a specialized SubAgent. "
            "Use this when a task is complex enough to benefit from a dedicated agent "
            "with its own tool access and reasoning loop. "
            "The SubAgent will execute the task and return results."
        ),
        args_schema=TaskToolInput,
    )
