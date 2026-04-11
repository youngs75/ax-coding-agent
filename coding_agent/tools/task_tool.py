"""Task tool — allows the main agent to delegate work to SubAgents."""

from __future__ import annotations

import asyncio
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


def build_task_tool(manager: SubAgentManager) -> StructuredTool:
    """Build a task delegation tool that captures manager via closure."""

    def _run_task(description: str, agent_type: str = "auto") -> str:
        """Spawn a SubAgent to handle the described task and return its output."""
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None and loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(
                        asyncio.run,
                        manager.spawn(description, agent_type=agent_type),
                    )
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

    return StructuredTool.from_function(
        func=_run_task,
        name="task",
        description=(
            "Delegate a task to a specialized SubAgent. "
            "Use this when a task is complex enough to benefit from a dedicated agent "
            "with its own tool access and reasoning loop."
        ),
        args_schema=TaskToolInput,
    )
