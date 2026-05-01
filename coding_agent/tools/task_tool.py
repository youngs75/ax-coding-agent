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
import contextvars
import json
import os
import re
import time
from typing import TYPE_CHECKING, Any, Callable

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


# R-003 (2026-04-27) — `\d{2,}` 가 `TASK-1`~`TASK-9` 1자리 ID 를 silently
# drop 했음. planner 가 zero-pad 를 해줄 거라는 *기대* 로 형식 강제하던 회피.
# 1자리 + N.M (sub-task) 모두 흡수해 LLM 출력 변형을 robust 하게 받는다.
_TASK_ID_PATTERN = re.compile(r"\bTASK-\d+(?:\.\d+)?\b", re.IGNORECASE)


def _extract_task_id(description: str) -> str | None:
    if not description:
        return None
    m = _TASK_ID_PATTERN.search(description)
    return m.group(0).upper() if m else None


# ---------------------------------------------------------------------------
# Auto-chain SubAgent invoke listener — surfaces verifier/fixer to the chat UI
# ---------------------------------------------------------------------------
# v22 #2 의 ``_auto_verify_chain`` 은 verifier/fixer 를 ``inner_func`` Python
# 호출로 직접 invoke (LangGraph tool 우회) — 따라서 ``astream_events`` 가
# 이 호출들을 발행하지 않아 chat UI 에서 verifier 가 실제로 호출됐는지 확인
# 불가. 사용자 보고: "verifier 나 fixer 는 왜 안 쓰지?" — 사실 호출됐지만
# wire 에 안 보였던 것.
#
# Web stream (``coding_agent.web.sse_emitter``) 이 stream 시작 시 contextvar
# 에 listener 를 등록하고, 이 모듈은 verifier/fixer invoke 전후로 listener 를
# 호출. listener 는 ``orchestrator.role.invoke.{start,end}`` SSE frame 으로
# 변환해서 chat UI 에 발행 → 사용자가 모든 SubAgent 협응을 볼 수 있다.
#
# CLI 모드는 listener 등록 안 함 (default ``None``) → emit 비용 0.
# ContextVar 사용 — multi-stream concurrent (서로 다른 asyncio task) 에서도
# 정확히 격리.

SubAgentInvokeListener = Callable[[str, str, "dict[str, Any]"], None]
"""(role, event_type "start"|"end", data) → None.

data keys:
  - start: ``description`` (str, ≤200 chars), ``attempt`` (int, 1-based)
  - end:   ``success`` (bool), ``elapsed_ms`` (int), ``attempt`` (int)
"""

_subagent_invoke_listener: contextvars.ContextVar[
    SubAgentInvokeListener | None
] = contextvars.ContextVar("task_tool_subagent_invoke_listener", default=None)


def set_subagent_invoke_listener(
    listener: SubAgentInvokeListener | None,
) -> contextvars.Token:
    """Register a listener for auto-chain (verifier/fixer) SubAgent invokes.

    Returns a ``Token`` for ``contextvars.ContextVar.reset`` so the caller
    can scope the listener to its own stream / request.

    web stream 이 verifier/fixer invoke 를 chat UI 로 surface 하기 위한 hook.
    CLI 모드는 호출 안 함.
    """
    return _subagent_invoke_listener.set(listener)


def _emit_subagent_invoke(role: str, event_type: str, data: dict[str, Any]) -> None:
    """Notify the registered listener if any. Swallow listener errors."""
    listener = _subagent_invoke_listener.get()
    if listener is None:
        return
    try:
        listener(role, event_type, data)
    except Exception:  # noqa: BLE001
        log.warning(
            "task_tool.subagent_invoke_listener.error",
            role=role,
            event_type=event_type,
            exc_info=True,
        )


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

# v22 #2 — auto-verify cycle bounds (Decision D: Soft 3 → Hard escalate)
# coder COMPLETED 이후 verifier 를 *자동* 1회 실행. verifier 가 실패 마커를
# 보고하면 fixer 를 호출하고 verifier 를 재실행. 최대 _AUTO_VERIFY_MAX_ATTEMPTS
# 회 verifier 시도 (= 첫 1회 + 재시도 2회). 모두 실패 시 결과 본문에
# AUTO_VERIFY_FAILED 마커를 붙여 orchestrator/critic 에 escalate 신호.
#
# v21 회귀의 직접 처방 — orchestrator LLM 이 reviewer/verifier 호출을 잊는
# 문제 (28 SubAgent 호출 중 verifier 0회) 를 *결정론적으로* 차단한다.
# Anthropic 의 "GANs for prose" 진단 + Osmani 의 generator/evaluator 분리
# 권고 + Codex 팀의 "invariants mechanical" 패턴의 합성.
_AUTO_VERIFY_MAX_ATTEMPTS = int(os.getenv("AX_AUTO_VERIFY_ATTEMPTS", "3"))
_AUTO_VERIFY_FAILED_MARKER = "[AUTO_VERIFY_FAILED]"
_AUTO_VERIFY_PASSED_MARKER = "[AUTO_VERIFY_PASSED]"


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

# Fixer 재시도 경고 임계값 — 같은 TASK-NN 의 fixer 호출 횟수가 이 값에
# 도달하면 strong warning 로그 발화. ProgressGuard 의 secondary repeat
# 한도 (6) 절반 수준. 환경변수 ``AX_FIXER_RETRY_WARN`` 으로 override.
_FIXER_RETRY_WARN_THRESHOLD = int(os.getenv("AX_FIXER_RETRY_WARN", "3"))

# v22.1 — outer loop bound. 같은 TASK-NN 의 fixer 호출이 hard cap 에 도달하면
# task_tool 자체가 inner_func 호출 *전* 에 short-circuit 으로 INCOMPLETE 반환.
# 이게 없으면 ProgressGuard 의 session-level safe_stop 까지 가서 *전체* 세션이
# 종료됨 (v22 회귀, 2026-04-26 — TASK-1.1 fixer 4회 후 stall_stop, TASK-1.2~13
# 도 같이 죽음). hard cap 은 task-level 격리 — 한 task 만 손해, 나머지 진행.
# Default 4 = WARN 임계값(3) 다음 1회 + ProgressGuard frequency=3 도달 전.
_FIXER_HARD_CAP = int(os.getenv("AX_FIXER_HARD_CAP", "4"))

# v22.4 — task_id 가 description 에 *없는* gate-level fixer 호출도 cap 으로
# 보호하기 위한 sentinel key. v25 회귀 — sufficiency.critic 이 fixer 위임할
# 때 description 에 ``TASK-NN`` prefix 가 없어 v22.1 의 task_id 기반 cap 이
# catch 못 함. 모든 task_id-free fixer 호출이 같은 sentinel 로 누적 → 무한
# loop 차단. (다행히 sufficiency.critic.loop_detection 이 v25 에선 잡았지만
# layer 별 다중 안전망이 정상.)
_FIXER_GATE_CAP_KEY = "__gate__"


def _build_auto_verifier_description(
    coder_description: str,
    coder_result: str,
) -> str:
    """v22 #2: orchestrator 가 보내준 coder description 을 verifier task 로 변환.

    verifier 가 실제 산출물을 *기계적*으로 확인하도록 명확한 지시:
    - read_file/glob_files 로 파일이 실제 작성됐는지 점검
    - 테스트 명령(pytest/npm test/jest 등)이 있으면 execute 로 실행
    - exit code 와 함께 결과 보고

    coder 결과 본문은 *말미 1500자만* 잘라 첨부 — 너무 길면 verifier 가
    원본 task 가 아닌 *coder 자가 보고* 만 보고 자가검증으로 빠진다
    ("GANs for prose" 회피).
    """
    coder_tail = coder_result[-1500:] if len(coder_result) > 1500 else coder_result
    return (
        "## verify TASK 산출물 (harness auto-invoke, v22 #2)\n\n"
        "### 원 task description (coder 가 받은 것)\n"
        f"{coder_description[:800]}\n\n"
        "### coder 보고 결과 (자가 보고 — 그대로 믿지 말 것)\n"
        f"{coder_tail}\n\n"
        "### 검증 지시 (반드시 결정론적 증거로 답할 것)\n"
        "1. coder 가 *실제로* 작성/수정했다고 주장하는 파일을 read_file 또는 "
        "glob_files 로 읽어 존재 + 내용 확인.\n"
        "2. 작성된 파일이 task 의 spec 을 만족하는지 점검.\n"
        "3. 테스트가 있으면 execute 로 실행 (pytest / npm test / jest / "
        "vitest / cargo test / go test 등 — 프로젝트 종류에 맞게).\n"
        "4. lint/build 명령이 있으면 함께 실행.\n"
        "5. 모든 execute 결과를 그대로 보고. exit code 0 = 통과, "
        "그 외 = 실패.\n\n"
        "검증 결과를 자연어로 *해석*하지 말 것 — 기계가 읽을 수 있게 "
        "execute 출력 그대로."
    )


def _build_auto_fixer_description(
    coder_description: str,
    verifier_result: str,
) -> str:
    """v22 #2: verifier 실패 후 fixer task description.

    `_prepend_verifier_evidence` 가 별도 wrapper 에서 verifier 결과를 prepend
    하지만, auto-chain 에서는 inner_func 직접 호출이라 wrapper 를 거치지
    않는다 → 여기서 직접 verifier 결과를 본문에 박아둔다.
    """
    verifier_tail = (
        verifier_result[-2500:] if len(verifier_result) > 2500 else verifier_result
    )
    return (
        "## fix TASK (harness auto-invoke, v22 #2)\n\n"
        "### 원 task description (coder 가 받았던 것)\n"
        f"{coder_description[:800]}\n\n"
        "### 직전 verifier 실패 증거\n"
        f"{verifier_tail}\n\n"
        "### fix 지시\n"
        "verifier 가 보고한 실패의 *근본 원인*을 고친다. workaround 금지. "
        "edit_file/write_file 로 코드 수정만 수행 — 테스트 실행은 다음 "
        "verifier 사이클이 담당."
    )


def _auto_verify_chain(
    *,
    inner_func: Any,
    coder_description: str,
    coder_result: str,
    base_tool_call_id: str,
) -> str:
    """v22 #2 — coder COMPLETED 직후 verifier+fixer 사이클 자동 실행.

    Decision D (Soft 3 → Hard escalate):
      - verifier 1차 → PASS 면 종료
      - FAIL 이면 fixer 호출 → verifier 2차 → PASS 면 종료
      - 또 FAIL 이면 fixer 2차 → verifier 3차
      - 3차도 FAIL 이면 ``[AUTO_VERIFY_FAILED]`` 마커 부착 후 반환

    inner_func 의 ``tool_call_id`` 는 stable 하게 유도 (LangGraph replay
    cache 가 같은 값으로 hit 하도록). orchestrator LLM 은 이 모든 사이클을
    *하나의* task() 호출 결과로 인식.
    """
    cycles_log: list[str] = []
    verifier_desc = _build_auto_verifier_description(coder_description, coder_result)
    last_verifier_text = ""

    for attempt in range(_AUTO_VERIFY_MAX_ATTEMPTS):
        verifier_id = f"{base_tool_call_id}-auto-verify-{attempt}"
        log.info(
            "task_tool.auto_verify.start",
            attempt=attempt + 1,
            max_attempts=_AUTO_VERIFY_MAX_ATTEMPTS,
        )
        # Surface to chat UI (auto-chain bypasses LangGraph tool layer).
        _emit_subagent_invoke(
            "verifier",
            "start",
            {"description": verifier_desc[:200], "attempt": attempt + 1},
        )
        verifier_started_at = time.monotonic()
        verifier_success = False
        try:
            verifier_text = inner_func(
                description=verifier_desc,
                agent_type="verifier",
                tool_call_id=verifier_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("task_tool.auto_verify.invoke_failed", attempt=attempt + 1)
            _emit_subagent_invoke(
                "verifier",
                "end",
                {
                    "success": False,
                    "elapsed_ms": int((time.monotonic() - verifier_started_at) * 1000),
                    "attempt": attempt + 1,
                },
            )
            cycles_log.append(
                f"### auto-verify attempt {attempt + 1} → INVOCATION ERROR\n{exc}"
            )
            break

        # Verifier 의 PASS 판정은 아래 분기와 *동일 로직* — 여기서 미리 평가해
        # invoke.end 의 success 필드에 정확히 반영. 이중 평가지만 비용 미미.
        # PASS 판정: COMPLETED 마커 + execute 실패 마커 부재.
        verifier_success = (
            "[Task COMPLETED" in verifier_text
            and not any(
                marker in verifier_text for marker in _EXECUTE_FAILURE_MARKERS
            )
        )
        _emit_subagent_invoke(
            "verifier",
            "end",
            {
                "success": verifier_success,
                "elapsed_ms": int((time.monotonic() - verifier_started_at) * 1000),
                "attempt": attempt + 1,
            },
        )

        last_verifier_text = verifier_text
        cycles_log.append(
            f"### auto-verify attempt {attempt + 1}\n{verifier_text}"
        )

        # PASS 판정: verifier 가 COMPLETED + execute 실패 마커 없음.
        # _verifier_signals_success 은 RoleInvocationResult 객체를 받지만
        # 여기선 이미 _format_result 가 통과한 *문자열* 만 가짐 →
        # 동일한 마커 ([exit code:, [TIMEOUT], REJECTED:, Error:) 를
        # 본문에서 검사한다. (signals.py 의 _extract_pytest_exit 가 같은
        # 로직을 사용 — 일관성 유지.)
        if "[Task COMPLETED" not in verifier_text:
            # verifier 자체가 INCOMPLETE/FAILED — 다음 사이클 진행
            pass
        elif not any(
            marker in verifier_text for marker in _EXECUTE_FAILURE_MARKERS
        ):
            log.info(
                "task_tool.auto_verify.passed",
                attempt=attempt + 1,
            )
            return (
                f"{coder_result}\n\n"
                f"## ↳ harness auto-verifier {_AUTO_VERIFY_PASSED_MARKER} "
                f"(v22 #2, attempt {attempt + 1}/{_AUTO_VERIFY_MAX_ATTEMPTS})\n"
                f"{verifier_text}"
            )

        # 마지막 사이클이면 fixer 호출 안 함
        if attempt >= _AUTO_VERIFY_MAX_ATTEMPTS - 1:
            break

        # fixer 호출
        fixer_desc = _build_auto_fixer_description(coder_description, verifier_text)
        fixer_id = f"{base_tool_call_id}-auto-fix-{attempt}"
        _emit_subagent_invoke(
            "fixer",
            "start",
            {"description": fixer_desc[:200], "attempt": attempt + 1},
        )
        fixer_started_at = time.monotonic()
        try:
            fixer_text = inner_func(
                description=fixer_desc,
                agent_type="fixer",
                tool_call_id=fixer_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("task_tool.auto_verify.fixer_failed", attempt=attempt + 1)
            _emit_subagent_invoke(
                "fixer",
                "end",
                {
                    "success": False,
                    "elapsed_ms": int((time.monotonic() - fixer_started_at) * 1000),
                    "attempt": attempt + 1,
                },
            )
            cycles_log.append(
                f"### auto-fix attempt {attempt + 1} → INVOCATION ERROR\n{exc}"
            )
            break
        _emit_subagent_invoke(
            "fixer",
            "end",
            {
                "success": "[Task COMPLETED" in fixer_text,
                "elapsed_ms": int((time.monotonic() - fixer_started_at) * 1000),
                "attempt": attempt + 1,
            },
        )

        cycles_log.append(f"### auto-fix attempt {attempt + 1}\n{fixer_text}")

        # 다음 verifier task description 에 직전 fixer 결과 첨부 (재검증 컨텍스트)
        verifier_desc = _build_auto_verifier_description(
            coder_description, f"{coder_result}\n\n## fixer 변경\n{fixer_text}"
        )

    # 모든 사이클 소진
    log.warning(
        "task_tool.auto_verify.exhausted",
        attempts=_AUTO_VERIFY_MAX_ATTEMPTS,
    )
    return (
        f"{coder_result}\n\n"
        f"## ↳ harness auto-verifier {_AUTO_VERIFY_FAILED_MARKER} "
        f"(v22 #2, {_AUTO_VERIFY_MAX_ATTEMPTS}회 시도 후 실패 — critic 검토 권장)\n"
        + "\n\n".join(cycles_log)
    )


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

    # Fixer retry counter per TASK-NN. ProgressGuard aborts at 6 same-key
    # repeats; we want the user to be *aware* well before the hard abort,
    # so when the same task hits ``_FIXER_RETRY_WARN_THRESHOLD`` (default 3)
    # consecutive fixer delegations the wrapper logs a high-visibility
    # warning and (later) can trigger an HITL panel. v8 RBAC verifier↔fixer
    # cycle 회귀 — 사용자가 6회 도달 후 abort 까지 기다리지 않고 *3 회 시점에
    # 인지할 기회*.
    _fixer_attempts: dict[str, int] = {}

    def _on_start(role_name: str, description: str) -> None:
        task_id = _extract_task_id(description)
        if task_id:
            _auto_advance_todo(
                todo_store, task_id, "in_progress", todo_change_callback
            )
        # Fixer 재시도 가드 — coder/verifier 같은 다른 role 호출이 끼어도
        # 같은 task_id 의 fixer 누적 카운트가 임계값에 도달하는 시점을
        # 검출. 카운트는 fixer 위임 시에만 증가, 다른 role 위임은 누적
        # 무시. v22.4 — task_id 가 없으면 ``_FIXER_GATE_CAP_KEY`` sentinel
        # 로 누적 (sufficiency.critic 발 gate-level fixer 도 cap 적용).
        if role_name == "fixer":
            cap_key = task_id or _FIXER_GATE_CAP_KEY
            count = _fixer_attempts.get(cap_key, 0) + 1
            _fixer_attempts[cap_key] = count
            if count >= _FIXER_RETRY_WARN_THRESHOLD:
                log.warning(
                    "task_tool.fixer_retry_warn",
                    task_id=task_id or "<gate>",
                    cap_key=cap_key,
                    attempts=count,
                    threshold=_FIXER_RETRY_WARN_THRESHOLD,
                    progress_guard_abort_at=6,
                    hint=(
                        "같은 task 를 fixer 가 반복 시도 중. 환경/요구사항이 "
                        "잘못 잡혔을 가능성 — 사용자 직접 검토 권장."
                    ),
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
        # v22.4 — coder 의 COMPLETED 만으로는 advance 하지 않는다. v25 회귀
        # ("CLI ✓ 의 거짓말") 차단: coder COMPLETED 후 _auto_verify_chain 이
        # 진입해 verifier PASS / FAIL 마커를 result_text 에 부착, _run_wrapped
        # 이 마커를 보고 ``completed`` (PASS) 또는 ``verify_failed`` (FAIL)
        # 로 별도 advance. 외부에서 verifier 우회로 직접 coder 호출하는
        # 경로는 advance 안 됨 — _auto_verify_chain 이 *반드시* 따라오므로
        # 정상 흐름엔 영향 없음.
        if role_name == "coder":
            return
        # v10 회귀 (기존 처방): planner/fixer/researcher/reviewer 가 처리한
        # task 도 ledger advance 필요. 그래야 task 가 ``in_progress`` 그대로
        # 남지 않음. verifier 만 ``execute`` 실패 마커가 있으면 마킹 보류
        # (테스트가 실제 통과한 경우만 ``completed`` — 기존 안전망).
        if role_name == "verifier":
            if not _verifier_signals_success(result):
                return
        # ledger / critic 은 task description 에 ``TASK-NN:`` 를 포함하지
        # 않아 ``_extract_task_id`` 가 None 을 돌려준다 → 위에서 이미 return.
        # 따라서 별도 가드 불필요.
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

        # v22.1 — outer loop bound. orchestrator 가 같은 TASK-NN 에 fixer 를
        # 무한 재호출하는 패턴 (v22 회귀) 차단. inner_func 호출 *전* 에
        # 짧은 INCOMPLETE 결과 반환 → orchestrator 가 다음 task 로 진행하도록.
        # 단, _on_start 의 _fixer_attempts 카운터는 inner_func 호출 시점에
        # 증가 → 여기선 *현재* 카운터를 보고 *다음 호출이 hard cap 도달 여부*
        # 판단. 즉 이미 cap 도달 = 직전 호출이 cap-1 회 = 이번이 cap+1 회.
        # v22.4 — task_id 가 없는 gate-level fixer 도 ``_FIXER_GATE_CAP_KEY``
        # sentinel 로 cap 적용. v25 회귀 (sufficiency.critic 발 fixer 무한
        # loop) layer-별 안전망.
        if resolved == "fixer":
            task_id_for_cap = _extract_task_id(description)
            cap_key = task_id_for_cap or _FIXER_GATE_CAP_KEY
            if _fixer_attempts.get(cap_key, 0) >= _FIXER_HARD_CAP:
                attempts = _fixer_attempts[cap_key]
                target_label = task_id_for_cap or "<gate-level>"
                log.warning(
                    "task_tool.fixer_hard_cap_reached",
                    task_id=task_id_for_cap,
                    cap_key=cap_key,
                    attempts=attempts,
                    cap=_FIXER_HARD_CAP,
                )
                return (
                    f"[Task INCOMPLETE — fixer]\n"
                    f"⚠ fixer for {target_label} hard-capped after "
                    f"{attempts} attempts (cap={_FIXER_HARD_CAP}).\n"
                    f"이 task 는 사용자 검토가 필요합니다 (자동 수정 한계 도달).\n"
                    f"**다음 pending task 로 진행하세요. 이 task 에 대해 fixer 를 "
                    f"다시 호출하지 마세요** — 같은 무한 루프 발생.\n"
                    f"[Duration: 0.0s]"
                )

        if resolved == "fixer" and _last_verifier_evidence["text"]:
            description = _prepend_verifier_evidence(
                description, _last_verifier_evidence["text"]
            )
            log.info(
                "task_tool.verifier_evidence_prepended",
                evidence_chars=len(_last_verifier_evidence["text"]),
            )

        result_text = inner_func(
            description=description,
            agent_type=agent_type,
            tool_call_id=tool_call_id,
        )

        # v22 #2 — auto-chain verifier+fixer after coder COMPLETED.
        # orchestrator LLM 이 verifier 호출을 잊는 v21 회귀를 차단한다.
        # 자세한 동기는 _AUTO_VERIFY_* 상수 위 주석 참조.
        if resolved == "coder" and "[Task COMPLETED" in result_text:
            result_text = _auto_verify_chain(
                inner_func=inner_func,
                coder_description=description,
                coder_result=result_text,
                base_tool_call_id=tool_call_id,
            )

            # v22.4 — chain 결과 마커로 todo advance. coder 의 _on_end 에서
            # 보류된 advance 를 여기서 결정 — PASS 면 completed, FAIL 이면
            # verify_failed. v25 회귀 ("CLI ✓ 의 거짓말") 의 마지막 처방.
            task_id_for_advance = _extract_task_id(description)
            if task_id_for_advance:
                if _AUTO_VERIFY_PASSED_MARKER in result_text:
                    _auto_advance_todo(
                        todo_store, task_id_for_advance,
                        "completed", todo_change_callback,
                    )
                elif _AUTO_VERIFY_FAILED_MARKER in result_text:
                    _auto_advance_todo(
                        todo_store, task_id_for_advance,
                        "verify_failed", todo_change_callback,
                    )

        return result_text

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
    "SubAgentInvokeListener",
    "_auto_advance_todo",
    "_extract_task_id",
    "_verifier_signals_success",
    "build_parallel_tasks_tool",
    "build_task_tool",
    "set_subagent_invoke_listener",
]
