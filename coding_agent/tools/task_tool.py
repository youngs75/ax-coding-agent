"""Task tool — thin ax wrapper over ``minyoung_mah.langgraph.build_subagent_task_tool``.

Phase 7 refactor (2026-04-19). The previous version owned the replay-safety
cache and the HITL propagation loop directly. Both now live in the library:

- **Replay safety** (``_TOOL_CALL_CACHE`` + ``replay_safe_tool_call``) —
  :mod:`minyoung_mah.langgraph.subagent_task_tool`.
- **HITL marker protocol** (``HITL_INTERRUPT_MARKER`` constant +
  ``extract_interrupt_payload``) — :mod:`minyoung_mah.hitl.interrupt`.

This module retains the ax-specific concerns the library stays out of:

- **Task classification** (``agent_type="auto"``) via :mod:`classifier`.
- **Todo auto-advance** on the ax-level :class:`TodoStore`
  (pre-flip to ``in_progress``, post-flip to ``completed`` for coder +
  successful verifier).
- **Verifier output formatting** — sanitizing and ``execute(command, result)``
  pairing so the orchestrator LLM sees evidence rather than continuation-style
  markdown sections.
- **Written-files footer** — cumulative list of ``write_file`` / ``edit_file``
  targets appended to successful terminal output.
- **Verifier→fixer evidence auto-prepend** — closure cache stores the most
  recent verifier ``_format_verifier_output`` text and a wrapper around the
  library tool prepends it to every fixer description. Replaces the prior
  prompt obligation ("verifier 가 보고한 실패를 fixer description 에 그대로
  복사하세요") so the orchestrator LLM no longer has to remember the copy
  step (v8 RBAC cycle regression — the LLM forgot under load and fixer
  re-tried the same broken patch). Harness-level observation, not prompt
  control.

These are plugged into the library via hooks (``resolve_role``,
``format_result``, ``on_tool_call_start``, ``on_tool_call_end``,
``on_user_answer``).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

import structlog
from langchain_core.tools import StructuredTool
from minyoung_mah.langgraph import build_subagent_task_tool
from pydantic import BaseModel, Field

from coding_agent.subagents.classifier import resolve_role_name
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
# Todo auto-advance (ax-specific — library stays out of the ledger)
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
# Verifier output formatting — strip instruction-style sections and append
# execute evidence pairs.
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


def _extract_text(result: "RoleInvocationResult") -> str:
    output = result.output
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, BaseModel):
        return output.model_dump_json()
    return str(output)


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
# build_task_tool — hooks into ``minyoung_mah.langgraph.build_subagent_task_tool``
# ---------------------------------------------------------------------------


# Length cap for the auto-prepended verifier evidence. Prevents a giant
# stack trace + log dump from drowning the fixer's actual task description.
# Empirically 8000 chars is enough for 5-10 pytest failures with full
# tracebacks; if a verifier emits more we truncate with a marker so the
# fixer at least knows there was overflow.
_VERIFIER_EVIDENCE_PREPEND_CAP = 8000


def _prepend_verifier_evidence(description: str, evidence: str) -> str:
    """Glue the last verifier's evidence onto a fixer description.

    Format intentionally matches what the orchestrator used to paste manually
    so existing fixer prompts/templates that key off "## 직전 verifier 결과"
    style headers still resonate. The "harness 자동 첨부" marker tells the
    fixer (and any debugger reading transcripts) this came from the wrapper,
    not the orchestrator LLM.
    """
    if len(evidence) > _VERIFIER_EVIDENCE_PREPEND_CAP:
        evidence = evidence[:_VERIFIER_EVIDENCE_PREPEND_CAP] + "\n... (truncated)"
    return (
        "## 직전 verifier 결과 (harness 자동 첨부)\n"
        f"{evidence}\n\n"
        "----\n\n"
        f"{description}"
    )


def build_task_tool(
    orchestrator: "Orchestrator",
    user_decisions: "UserDecisionsLog",
    todo_store: "TodoStore | None" = None,
    todo_change_callback: Any | None = None,
) -> StructuredTool:
    """Build a ``task`` tool bound to a minyoung_mah Orchestrator.

    The library owns the replay-safety cache, HITL interrupt propagation,
    and the role-invocation loop. This wrapper injects ax-specific hooks:

    * ``resolve_role`` → classifier's ``resolve_role_name``
    * ``on_tool_call_start`` → flip matching TASK-NN todo to ``in_progress``
    * ``on_tool_call_end`` → flip to ``completed`` for successful coder runs
      and verifier runs whose ``execute`` calls all succeeded
    * ``format_result`` → verifier evidence formatting + written-files
      footer + duration tag
    * ``format_hitl_answer`` / ``on_user_answer`` → ask_tool formatter +
      :class:`UserDecisionsLog` persistence

    Fixer is NOT auto-advanced — fix success/failure is judged by the next
    verifier round, so the orchestrator LLM keeps the call.

    On top of the library tool we add a thin StructuredTool wrapper that
    auto-prepends the most recent verifier's evidence to any fixer
    delegation. The library tool itself stays unchanged; the wrapper shares
    the same args_schema and forwards to the inner ``func`` so LangGraph's
    ``InjectedToolCallId`` plumbing keeps working.
    """

    # Closure cache for the most-recent verifier evidence text. Mutable
    # container (dict) so the inner ``_on_end`` closure can rebind without
    # ``nonlocal``. ``_run_wrapped`` reads it on every fixer delegation.
    _last_verifier_evidence: dict[str, str] = {"text": ""}

    def _on_start(role_name: str, description: str) -> None:
        task_id = _extract_task_id(description)
        if task_id:
            _auto_advance_todo(
                todo_store, task_id, "in_progress", todo_change_callback
            )

    def _on_end(
        role_name: str,
        description: str,
        result: "RoleInvocationResult",
        status_tag: str,
    ) -> None:
        # Cache verifier evidence regardless of COMPLETED/INCOMPLETE — fixer
        # is most useful exactly when verifier surfaced something. We only
        # skip on hard FAILED/ABORTED where ``_format_verifier_output`` may
        # not have meaningful execute pairs to extract.
        if role_name == "verifier" and status_tag in ("COMPLETED", "INCOMPLETE"):
            try:
                _last_verifier_evidence["text"] = _format_verifier_output(result)
            except Exception:
                log.exception("task_tool.verifier_evidence_cache_failed")
                _last_verifier_evidence["text"] = ""

        if status_tag != "COMPLETED":
            return
        task_id = _extract_task_id(description)
        if not task_id:
            return
        if role_name == "coder" or (
            role_name == "verifier" and _verifier_signals_success(result)
        ):
            _auto_advance_todo(
                todo_store, task_id, "completed", todo_change_callback
            )

    def _format_result(
        *,
        role_name: str,
        description: str,  # noqa: ARG001
        result: "RoleInvocationResult",
        elapsed_s: float,
        status_tag: str,
    ) -> str:
        if role_name == "verifier":
            body = _format_verifier_output(result)
        else:
            body = _extract_text(result)
        written = _extract_written_files(result)
        parts = [f"[Task {status_tag} — {role_name}]", body]
        if written:
            parts.append(f"Files written: {', '.join(written)}")
        parts.append(f"[Duration: {elapsed_s:.1f}s]")
        return "\n".join(parts)

    inner_tool = build_subagent_task_tool(
        orchestrator,
        resolve_role=resolve_role_name,
        format_result=_format_result,
        format_hitl_answer=_format_answer,
        on_tool_call_start=_on_start,
        on_tool_call_end=_on_end,
        on_user_answer=user_decisions.record,
        tool_name="task",
        tool_description=(
            "Delegate a task to a specialized SubAgent. "
            "Use this when a task is complex enough to benefit from a dedicated "
            "agent with its own tool access and reasoning loop."
        ),
    )

    inner_func = inner_tool.func
    if inner_func is None:  # pragma: no cover — minyoung_mah always sets func
        return inner_tool

    def _run_wrapped(
        description: str,
        agent_type: str = "auto",
        tool_call_id: str = "",
    ) -> str:
        # Resolve the role *before* invoking the inner tool so we know whether
        # to prepend evidence. ``resolve_role_name`` is the same function the
        # inner tool uses internally — running it twice is cheap (keyword
        # fast-path or a single fast-LLM classify call) and keeps the
        # prepend decision local to this wrapper.
        try:
            resolved = resolve_role_name(agent_type, description)
        except Exception:
            log.exception("task_tool.resolve_role_failed_in_wrapper")
            resolved = agent_type

        if resolved == "fixer" and _last_verifier_evidence["text"]:
            description = _prepend_verifier_evidence(
                description, _last_verifier_evidence["text"]
            )
            log.info(
                "task_tool.verifier_evidence_prepended",
                evidence_chars=len(_last_verifier_evidence["text"]),
            )

        return inner_func(
            description=description,
            agent_type=agent_type,
            tool_call_id=tool_call_id,
        )

    # Re-wrap with the same args_schema so InjectedToolCallId, name, and
    # description all match what callers expect.
    return StructuredTool.from_function(
        func=_run_wrapped,
        name=inner_tool.name,
        description=inner_tool.description,
        args_schema=inner_tool.args_schema,
    )


# ---------------------------------------------------------------------------
# build_parallel_tasks_tool — optional, currently unused by the top loop
# ---------------------------------------------------------------------------


class ParallelTasksInput(BaseModel):
    tasks: str = Field(
        description=(
            'JSON array of task objects. Each object must have "description" (str) '
            'and optionally "agent_type" (str).'
        ),
    )


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

            def _invoke_one(t: dict[str, Any], idx: int) -> Any:
                # InjectedToolCallId on the single_tool schema requires a full
                # ToolCall envelope — synthesize a synthetic id per parallel
                # slot so replay-safety caches don't collide across workers.
                return single_tool.invoke(
                    {
                        "name": "task",
                        "args": {
                            "description": t["description"],
                            "agent_type": t.get("agent_type", "auto"),
                        },
                        "type": "tool_call",
                        "id": f"parallel-{idx}",
                    }
                )

            if len(task_list) == 1:
                result = _invoke_one(task_list[0], 0)
                return result if isinstance(result, str) else result.content

            async def _run_all() -> list[Any]:
                return await asyncio.gather(
                    *(
                        asyncio.to_thread(_invoke_one, t, i)
                        for i, t in enumerate(task_list)
                    )
                )

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None and loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    outputs = pool.submit(asyncio.run, _run_all()).result(timeout=600)
            else:
                outputs = asyncio.run(_run_all())

            parts: list[str] = []
            for i, out in enumerate(outputs):
                text = out if isinstance(out, str) else out.content
                header = f"## Task {i + 1}: {task_list[i]['description'][:60]}"
                parts.append(f"{header}\n{text}")
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
    "_auto_advance_todo",
    "_extract_task_id",
    "_verifier_signals_success",
    "build_parallel_tasks_tool",
    "build_task_tool",
]
