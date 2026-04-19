"""Task tool — orchestrates SubAgent delegation through minyoung_mah.Orchestrator.

Phase 6 refactor. The previous implementation called ``SubAgentManager.spawn``
which owned its own inner LangGraph. This version delegates to
``Orchestrator.invoke_role`` from the minyoung-mah library. Key responsibilities
retained at this layer (the library deliberately stays out of them):

- **Task classification** (``agent_type="auto"``) via :mod:`classifier`.
- **Todo auto-advance** on the ax-level :class:`TodoStore`.
- **Interrupt propagation** (plan §결정 3): the ask_user_question adapter
  returns an ``__ax_interrupt__`` marker; we surface it via LangGraph
  ``interrupt()`` and resume the role with the user's answer in
  ``parent_outputs["previous_ask"]``.
- **Verifier output formatting** — `execute(command, result)` pairing.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re
import time
from typing import TYPE_CHECKING, Annotated, Any

import structlog
from langchain_core.tools import InjectedToolCallId, StructuredTool
from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from coding_agent.subagents.classifier import resolve_role_name
from coding_agent.tools.ask_adapter import extract_interrupt_payload
from coding_agent.tools.ask_tool import _format_answer

if TYPE_CHECKING:
    from minyoung_mah import Orchestrator, RoleInvocationResult

    from coding_agent.subagents.user_decisions import UserDecisionsLog
    from coding_agent.tools.todo_tool import TodoStore

log = structlog.get_logger("task_tool")


# ---------------------------------------------------------------------------
# TASK-NN id extraction (shared with ProgressGuard key_extractor)
# ---------------------------------------------------------------------------


_TASK_ID_PATTERN = re.compile(r"\bTASK-\d{2,}\b", re.IGNORECASE)


def _extract_task_id(description: str) -> str | None:
    if not description:
        return None
    m = _TASK_ID_PATTERN.search(description)
    return m.group(0).upper() if m else None


# ---------------------------------------------------------------------------
# Async runner (LangGraph nodes are sync; role invocation is async)
# ---------------------------------------------------------------------------


# One shared pool across all task_tool invocations to avoid the overhead
# of spinning up a thread + event loop per SubAgent call.
_shared_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)


# Module-level cache for LangGraph interrupt replay-safety.
# Keyed by ``tool_call_id`` (injected by LangGraph per tool call), inner dict
# maps ``iter_idx`` → ``RoleInvocationResult`` so replayed executions of the
# same tool call reuse the exact same role invocation result instead of
# re-calling the nondeterministic LLM. Entries are cleared when a tool call
# reaches a terminal state (both success and failure paths).
_TOOL_CALL_CACHE: dict[str, dict[int, Any]] = {}


def _run_async(coro: Any) -> Any:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        future = _shared_pool.submit(asyncio.run, coro)
        return future.result(timeout=600)
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tool input schemas
# ---------------------------------------------------------------------------


class TaskToolInput(BaseModel):
    description: str = Field(
        description="A detailed description of the task to delegate to a SubAgent."
    )
    agent_type: str = Field(
        default="auto",
        description=(
            "The type of SubAgent to spawn. "
            "Options: 'planner', 'coder', 'reviewer', 'fixer', 'researcher', "
            "'verifier', 'ledger', 'auto'. "
            "Use 'auto' to let the system choose."
        ),
    )
    # Injected by LangGraph at call time; excluded from LLM schema so the model
    # does not try to supply it. Used as cache key for interrupt replay-safety
    # inside ``_run_task`` (see P1 LangGraph replay pitfall).
    tool_call_id: Annotated[str, InjectedToolCallId] = ""


class ParallelTasksInput(BaseModel):
    tasks: str = Field(
        description=(
            'JSON array of task objects. Each object must have "description" (str) '
            'and optionally "agent_type" (str).'
        ),
    )


# ---------------------------------------------------------------------------
# Todo auto-advance (moved from SubAgentManager.auto_advance_todo)
# ---------------------------------------------------------------------------


def _auto_advance_todo(
    todo_store: "TodoStore | None",
    task_id: str | None,
    status: str,
    todo_change_callback: Any | None,
) -> bool:
    if todo_store is None or not task_id:
        return False
    try:
        current = todo_store.list_items()
    except Exception:
        return False
    match = next((it for it in current if it.id == task_id), None)
    if match is None:
        return False
    if match.status == status:
        return False
    if match.status == "completed" and status != "completed":
        return False
    try:
        todo_store.update(task_id, status)  # type: ignore[arg-type]
    except KeyError:
        return False
    if todo_change_callback is not None:
        try:
            todo_change_callback(todo_store.list_items())
        except Exception:
            pass
    log.info("task_tool.todo.auto_advance", task_id=task_id, status=status)
    return True


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------


_VERIFIER_SUMMARY_HEAD_LIMIT = 400
_VERIFIER_FORBIDDEN_HEADINGS = (
    "## Error Report",
    "## Fixer Instructions",
    "## Success Criteria",
    "## Fix Plan",
    "## Recommendations",
)


def _sanitize_verifier_text(text: str) -> str:
    """Drop instruction-style markdown sections that would steer the top-level
    LLM into continuation mode (pasting "## Fixer Instructions" verbatim
    instead of calling ``task(fixer)``).

    We keep only the prefix up to the first forbidden heading, then truncate
    to ``_VERIFIER_SUMMARY_HEAD_LIMIT`` chars. The structured evidence the
    orchestrator actually needs lives in the execute(command, result) pairs
    appended separately.
    """
    if not text:
        return ""
    cut = len(text)
    for heading in _VERIFIER_FORBIDDEN_HEADINGS:
        idx = text.find(heading)
        if idx != -1 and idx < cut:
            cut = idx
    head = text[:cut].rstrip()
    if len(head) > _VERIFIER_SUMMARY_HEAD_LIMIT:
        head = head[:_VERIFIER_SUMMARY_HEAD_LIMIT].rstrip() + " … (truncated)"
    return head


def _format_verifier_output(result: "RoleInvocationResult") -> str:
    """Emit only the machine-readable verifier evidence.

    Composition:
      1. Sanitized head of the verifier's own summary (Scope/Result/Issues).
         Instruction-style sections ("## Fixer Instructions" etc.) are
         stripped — they hijack the orchestrator LLM into a continuation
         response with no tool_calls (observed in v10 E2E).
      2. ``execute(command, result)`` pairs for every shell invocation —
         this is the concrete evidence fixer needs (test names, exit codes,
         stack traces).
    """
    lines: list[str] = []
    text = _sanitize_verifier_text(_extract_text(result))
    if text:
        lines.append(text)

    executes = [
        (req, res)
        for req, res in zip(result.tool_calls or [], result.tool_results or [])
        if req.tool_name == "execute"
    ]
    if executes:
        lines.append("")
        lines.append("### execute(command, result) pairs")
        for req, res in executes:
            cmd = req.args.get("command", "") if isinstance(req.args, dict) else ""
            value = res.value if res.ok else (res.error or "")
            lines.append(f"- command: {cmd}")
            if value:
                snippet = (
                    value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                )
                snippet = snippet.strip()
                if len(snippet) > 800:
                    snippet = snippet[:800] + "\n... (truncated)"
                lines.append(f"  result: {snippet}")
    return "\n".join(lines)


def _extract_text(result: "RoleInvocationResult") -> str:
    output = result.output
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, BaseModel):
        return output.model_dump_json()
    return str(output)


def _extract_written_files(result: "RoleInvocationResult") -> list[str]:
    written: list[str] = []
    for req in result.tool_calls or []:
        if req.tool_name not in ("write_file", "edit_file"):
            continue
        if isinstance(req.args, dict):
            path = req.args.get("path")
            if isinstance(path, str) and path not in written:
                written.append(path)
    return written


def _find_pending_interrupt(result: "RoleInvocationResult") -> dict[str, Any] | None:
    """Return the first ``__ax_interrupt__`` payload in the role's tool
    results, or None. The marker is inserted by
    ``coding_agent.tools.ask_adapter.AskUserQuestionAdapter``.
    """
    for res in result.tool_results or []:
        if not res.ok:
            continue
        payload = extract_interrupt_payload(res.value)
        if payload is not None:
            return payload
    return None


# ---------------------------------------------------------------------------
# Verifier success detection — gate for auto-advancing todos on verifier path
# ---------------------------------------------------------------------------

# coding_agent.tools.shell.execute never raises; non-zero exits, timeouts,
# guard rejections, and adapter errors are all encoded as substrings in the
# returned text. So ToolResult.ok=True alone is not a strong enough signal —
# we also scan the value for these markers before declaring "verified".
_EXECUTE_FAILURE_MARKERS = (
    "[exit code:",
    "[TIMEOUT]",
    "REJECTED:",
    "Error:",
)


def _verifier_signals_success(result: "RoleInvocationResult") -> bool:
    """True iff the verifier ran at least one ``execute`` call and every
    one of them succeeded — both at the adapter layer (``ok=True``) and
    at the shell layer (no failure markers in the captured output).

    Why both checks: ``execute`` swallows non-zero exit codes and prints
    them as ``[exit code: N]`` text instead of raising. A test failure
    therefore round-trips as ``ToolResult(ok=True, value="...[exit code: 1]")``,
    so ``ok`` alone would auto-advance even when tests fail.
    """
    saw_execute = False
    for req, res in zip(result.tool_calls or [], result.tool_results or []):
        if req.tool_name != "execute":
            continue
        saw_execute = True
        if not res.ok:
            return False
        text = res.value if isinstance(res.value, str) else ""
        if any(marker in text for marker in _EXECUTE_FAILURE_MARKERS):
            return False
    return saw_execute


# ---------------------------------------------------------------------------
# build_task_tool — the LangGraph tool the orchestrator LLM sees
# ---------------------------------------------------------------------------


def build_task_tool(
    orchestrator: "Orchestrator",
    user_decisions: "UserDecisionsLog",
    todo_store: "TodoStore | None" = None,
    todo_change_callback: Any | None = None,
) -> StructuredTool:
    """Build a ``task`` tool bound to a minyoung_mah Orchestrator.

    ``orchestrator`` is the one assembled in
    :func:`coding_agent.subagents.orchestrator_factory.build_orchestrator`.
    ``user_decisions`` carries ``ask_user_question`` answers across SubAgent
    invocations so coder/verifier/fixer all see the same hard constraints.

    Interrupt replay-safety (LangGraph 공식 경고)
    ---------------------------------------------
    ``interrupt()`` causes the entire tool function to be *re-executed* on
    resume — every side effect before the interrupt runs again. ``_run_task``
    pairs each iteration's ``orchestrator.invoke_role`` (nondeterministic LLM
    call) with an ``interrupt()`` call, so a naive implementation drifts by
    one round on every resume (new pending generated on replay gets matched
    to the previously saved answer).

    To stay compliant with LangGraph's replay semantics we memoise each
    iteration's ``RoleInvocationResult`` in ``_TOOL_CALL_CACHE`` keyed by
    ``(tool_call_id, iter_idx)``:

    * fresh call  → cache miss → invoke → cache write → interrupt
    * replay      → cache hit  → skip invoke → interrupt returns stored
                    answer immediately → loop proceeds deterministically

    The entry is cleared in ``finally`` so tool calls that eventually
    terminate do not leak cache memory.
    """

    def _run_task(
        description: str,
        agent_type: str = "auto",
        tool_call_id: str = "",
    ) -> str:
        from minyoung_mah import InvocationContext, RoleStatus

        t0 = time.monotonic()
        role_name = resolve_role_name(agent_type, description)
        log.info(
            "timing.task_tool.start",
            agent_type=agent_type,
            role=role_name,
            desc=description[:80],
            tool_call_id=tool_call_id[:16] if tool_call_id else "",
        )

        # B-1: flip the ledger to in_progress before delegation.
        task_id = _extract_task_id(description)
        if task_id:
            _auto_advance_todo(todo_store, task_id, "in_progress", todo_change_callback)

        parent_outputs: dict[str, Any] = {}
        # Replay-safety cache for this tool call. tool_call_id is injected by
        # LangGraph; anonymous fallback "" still works for non-LangGraph tests
        # (each test gets its own slot since the finally block clears it).
        cache_bucket: dict[int, Any] = _TOOL_CALL_CACHE.setdefault(tool_call_id, {})
        iter_idx = 0

        try:
            while True:
                cached = cache_bucket.get(iter_idx)
                if cached is not None:
                    result = cached
                    log.debug(
                        "task_tool.replay_cache_hit",
                        role=role_name,
                        iter_idx=iter_idx,
                    )
                else:
                    ctx = InvocationContext(
                        task_summary=description,
                        user_request="",
                        parent_outputs=dict(parent_outputs),
                    )
                    result = _run_async(orchestrator.invoke_role(role_name, ctx))
                    cache_bucket[iter_idx] = result

                # HITL path (plan §결정 3) — role reported an ask via marker.
                pending = _find_pending_interrupt(result)
                if pending is not None:
                    log.info(
                        "task_tool.propagate_interrupt",
                        role=role_name,
                        payload_preview=str(pending)[:120],
                        iter_idx=iter_idx,
                    )
                    user_answer = interrupt(pending)
                    log.info(
                        "task_tool.received_answer",
                        answer_preview=str(user_answer)[:80],
                        iter_idx=iter_idx,
                    )
                    formatted = _format_answer(pending, user_answer)
                    user_decisions.record(formatted)
                    parent_outputs["previous_ask"] = formatted
                    iter_idx += 1
                    continue  # re-invoke with the answer prepended

                # Terminal — either COMPLETED / INCOMPLETE / FAILED / ABORTED.
                break

            elapsed = time.monotonic() - t0
            log.info(
                "timing.task_tool.done",
                role=role_name,
                status=result.status.name,
                duration_s=round(elapsed, 1),
                role_duration_ms=result.duration_ms,
                iterations=result.iterations,
            )

            if result.status in (RoleStatus.COMPLETED, RoleStatus.INCOMPLETE):
                status_tag = (
                    "INCOMPLETE" if result.status is RoleStatus.INCOMPLETE else "COMPLETED"
                )
                if role_name == "verifier":
                    body = _format_verifier_output(result)
                else:
                    body = _extract_text(result)

                # B-1: coder success → mark completed.
                # P2: verifier success (COMPLETED + every execute call ok and
                # without failure markers) → mark completed. Fixer never
                # auto-advances — fix success/failure is judged by the next
                # verifier round, so the orchestrator LLM keeps the call.
                if task_id and status_tag == "COMPLETED" and (
                    role_name == "coder"
                    or (
                        role_name == "verifier"
                        and _verifier_signals_success(result)
                    )
                ):
                    _auto_advance_todo(
                        todo_store, task_id, "completed", todo_change_callback
                    )

                written = _extract_written_files(result)
                parts = [f"[Task {status_tag} — {role_name}]", body]
                if written:
                    parts.append(f"Files written: {', '.join(written)}")
                parts.append(f"[Duration: {elapsed:.1f}s]")
                _TOOL_CALL_CACHE.pop(tool_call_id, None)
                return "\n".join(parts)

            # FAILED / ABORTED
            err = result.error or f"role '{role_name}' terminated with {result.status.name}"
            _TOOL_CALL_CACHE.pop(tool_call_id, None)
            return f"SubAgent failed: {err}"

        except GraphInterrupt:
            # Do NOT clear cache — LangGraph will replay this tool call on
            # resume and the stored invocation results are what make the
            # replay return the previously-saved answers deterministically.
            raise
        except Exception as exc:
            _TOOL_CALL_CACHE.pop(tool_call_id, None)
            return f"Error running SubAgent: {exc}"

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


# ---------------------------------------------------------------------------
# build_parallel_tasks_tool — optional, currently unused by the top loop
# ---------------------------------------------------------------------------


def build_parallel_tasks_tool(
    orchestrator: "Orchestrator",
    user_decisions: "UserDecisionsLog",
    todo_store: "TodoStore | None" = None,
    todo_change_callback: Any | None = None,
) -> StructuredTool:
    """Parallel variant — runs each task through :func:`build_task_tool`
    concurrently. Interrupt propagation is NOT supported in the parallel
    path (it would serialize users across N simultaneous asks), so the
    orchestrator LLM should avoid this tool for tasks that might call
    ``ask_user_question``. Kept for future activation once dependency
    analysis lands — currently AgentLoop does not bind it.
    """
    single_tool = build_task_tool(
        orchestrator, user_decisions, todo_store, todo_change_callback
    )

    def _run_parallel_tasks(tasks: str) -> str:
        try:
            task_list = json.loads(tasks)
            if not isinstance(task_list, list) or len(task_list) == 0:
                return "Error: tasks must be a non-empty JSON array."
            if len(task_list) == 1:
                t = task_list[0]
                return single_tool.invoke(
                    {
                        "description": t["description"],
                        "agent_type": t.get("agent_type", "auto"),
                    }
                )

            async def _run_all() -> list[str]:
                return await asyncio.gather(
                    *(
                        asyncio.to_thread(
                            single_tool.invoke,
                            {
                                "description": t["description"],
                                "agent_type": t.get("agent_type", "auto"),
                            },
                        )
                        for t in task_list
                    )
                )

            outputs = _run_async(_run_all())
            parts: list[str] = []
            for i, out in enumerate(outputs):
                header = f"## Task {i + 1}: {task_list[i]['description'][:60]}"
                parts.append(f"{header}\n{out}")
            return "\n\n".join(parts)
        except json.JSONDecodeError as exc:
            return f"Error: invalid JSON in tasks parameter — {exc}"
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


__all__ = [
    "build_task_tool",
    "build_parallel_tasks_tool",
    "_extract_task_id",
    "_auto_advance_todo",
    "_verifier_signals_success",
]
