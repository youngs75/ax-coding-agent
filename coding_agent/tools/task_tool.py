"""Task tool — allows the main agent to delegate work to SubAgents."""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

from langchain_core.tools import StructuredTool
from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from coding_agent.subagents.manager import SubAgentManager


# Match the *first* TASK-NN-style identifier appearing in a delegation
# description so the manager can auto-advance the todo ledger without
# requiring the orchestrator LLM to remember update_todo calls. We
# anchor on word boundaries so substrings like "TASK-04-fixup" still
# resolve to TASK-04, and we accept 2+ digit ids to be forward-compatible.
_TASK_ID_PATTERN = re.compile(r"\bTASK-\d{2,}\b", re.IGNORECASE)


def _extract_task_id(description: str) -> str | None:
    """Return the first TASK-NN id found in *description*, or None.

    Always normalised to upper case so the lookup matches the canonical
    ids stored in TodoStore (which echo whatever the planner wrote).
    """
    if not description:
        return None
    m = _TASK_ID_PATTERN.search(description)
    if m is None:
        return None
    return m.group(0).upper()


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


class ParallelTasksInput(BaseModel):
    """Input schema for the ``parallel_tasks`` tool."""

    tasks: str = Field(
        description=(
            'JSON array of task objects. Each object must have "description" (str) '
            'and optionally "agent_type" (str). '
            'Example: [{"description": "Create models.py", "agent_type": "coder"}, '
            '{"description": "Create views.py", "agent_type": "coder"}]'
        ),
    )


import concurrent.futures

# Reuse a single thread pool across all task tool invocations to avoid
# the overhead of creating a new thread + event loop for every SubAgent call.
_shared_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)


def _run_async(coro):
    """Run an async coroutine from sync context, handling running loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        future = _shared_pool.submit(asyncio.run, coro)
        return future.result(timeout=600)
    else:
        return asyncio.run(coro)


def build_task_tool(manager: SubAgentManager) -> StructuredTool:
    """Build a task delegation tool that captures manager via closure."""
    import time as _time
    import structlog as _structlog
    _log = _structlog.get_logger("task_tool")

    def _run_task(description: str, agent_type: str = "auto") -> str:
        """Spawn a SubAgent to handle the described task and return its output.

        Interrupt-aware: if the SubAgent pauses on a LangGraph interrupt
        (e.g. ask_user_question called from a planner), this function
        propagates the interrupt up to the orchestrator graph by calling
        ``interrupt()`` itself. The orchestrator pauses too, the user
        answers via the CLI, and on resume the *same* SubAgent run picks
        up where it left off — no re-spawn, no duplicate LLM call.
        """

        def _spawn() -> Any:
            return _run_async(manager.spawn(description, agent_type=agent_type))

        def _resume(answer: Any) -> Any:
            return _run_async(
                manager.spawn(
                    description,
                    agent_type=agent_type,
                    resume_value=answer,
                )
            )

        try:
            t0 = _time.monotonic()
            _log.info(
                "timing.task_tool.start",
                agent_type=agent_type,
                desc=description[:80],
            )

            # B-1: auto-advance the todo ledger so the orchestrator LLM
            # does not have to remember update_todo on every delegation.
            # Silently no-ops if the id is not in the ledger.
            task_id = _extract_task_id(description)
            if task_id:
                manager.auto_advance_todo(task_id, "in_progress")

            result = _spawn()

            # ── Interrupt propagation loop ──
            # The SubAgent may pause on any number of ask_user_question
            # interrupts. Each one is forwarded up to the orchestrator
            # graph via interrupt(); the resume value comes back here
            # and we hand it to the SubAgent via _resume().
            while result.interrupt_payload is not None:
                _log.info(
                    "task_tool.propagate_interrupt",
                    agent_type=agent_type,
                    thread_id=result.thread_id,
                )
                # interrupt() raises GraphInterrupt the first time the
                # tool node runs; the second time (after Command(resume=))
                # it returns the answer immediately.
                user_answer = interrupt(result.interrupt_payload)
                _log.info(
                    "task_tool.received_answer",
                    answer_preview=str(user_answer)[:80],
                )
                result = _resume(user_answer)

            elapsed = _time.monotonic() - t0

            _log.info(
                "timing.task_tool.done",
                success=result.success,
                duration_s=round(result.duration_s, 1),
                roundtrip_s=round(elapsed, 1),
                overhead_s=round(elapsed - result.duration_s, 1),
                files=len(result.written_files),
            )

            if result.success:
                # Fix 3: Return structured summary, not raw SubAgent output.
                # Distinguish COMPLETED vs INCOMPLETE so the Orchestrator
                # knows whether to continue the same phase or move on.
                is_incomplete = "[INCOMPLETE" in result.output
                status = "INCOMPLETE" if is_incomplete else "COMPLETED"
                # B-1: when a coder finishes successfully (and not flagged
                # as incomplete), promote the todo to completed. verifier/
                # fixer keep it at in_progress so the cycle is visible.
                if (
                    task_id
                    and not is_incomplete
                    and agent_type in ("coder", "auto")
                ):
                    manager.auto_advance_todo(task_id, "completed")
                parts = [
                    f"[Task {status} — {agent_type}]",
                    result.output,
                ]
                if result.written_files:
                    parts.append(f"Files written: {', '.join(result.written_files)}")
                parts.append(f"[Duration: {result.duration_s:.1f}s]")
                return "\n".join(parts)
            else:
                return f"SubAgent failed: {result.error or 'unknown error'}"

        except GraphInterrupt:
            # Must propagate so the outer graph pauses for the user.
            raise
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


def build_parallel_tasks_tool(manager: SubAgentManager) -> StructuredTool:
    """Build a parallel task delegation tool that runs multiple SubAgents concurrently."""

    def _run_parallel_tasks(tasks: str) -> str:
        """Spawn multiple SubAgents in parallel and return combined results."""
        try:
            task_list = json.loads(tasks)
            if not isinstance(task_list, list) or len(task_list) == 0:
                return "Error: tasks must be a non-empty JSON array."
            if len(task_list) == 1:
                # Single task — just use normal spawn
                t = task_list[0]
                return _run_async(
                    manager.spawn(t["description"], agent_type=t.get("agent_type", "auto"))
                ).__str__()

            results = _run_async(manager.spawn_parallel(task_list))

            parts: list[str] = []
            total_duration = 0.0
            all_files: list[str] = []
            failures = 0
            for i, r in enumerate(results):
                header = f"## Task {i + 1}: {task_list[i]['description'][:60]}"
                if r.success:
                    parts.append(f"{header}\nStatus: SUCCESS ({r.duration_s:.1f}s)")
                    if r.written_files:
                        all_files.extend(r.written_files)
                        parts.append(f"Files: {', '.join(r.written_files)}")
                    parts.append(r.output)
                else:
                    failures += 1
                    parts.append(f"{header}\nStatus: FAILED — {r.error}")
                total_duration = max(total_duration, r.duration_s)

            summary = (
                f"\n---\nParallel execution: {len(results)} tasks, "
                f"{len(results) - failures} succeeded, {failures} failed, "
                f"wall time {total_duration:.1f}s"
            )
            if all_files:
                summary += f"\nAll files written: {', '.join(all_files)}"
            parts.append(summary)
            return "\n\n".join(parts)

        except json.JSONDecodeError as e:
            return f"Error: invalid JSON in tasks parameter — {e}"
        except Exception as exc:
            return f"Error running parallel tasks: {exc}"

    return StructuredTool.from_function(
        func=_run_parallel_tasks,
        name="parallel_tasks",
        description=(
            "Run multiple independent SubAgent tasks in parallel. "
            "Use this when you have several tasks that don't depend on each other "
            "(e.g., creating separate files). Much faster than sequential task calls. "
            "Tasks that modify the same file should NOT be parallelized."
        ),
        args_schema=ParallelTasksInput,
    )
