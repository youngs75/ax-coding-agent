"""Deterministic 신호 추출 — agent state / messages / todo_store / 디스크에서
sufficiency rule_gate 가 사용할 수치를 모은다.

apt-legal 은 SynthesizerOutput.answer 텍스트와 PipelineStepResult 를 보지만
ax 는 LangGraph 메시지 기반이므로:

- ``todo_*``    : ``TodoStore.counts()`` 직접 (주입 받음)
- ``pytest_exit``: 마지막 verifier ``ToolMessage`` 의 ``execute(command, result)``
                  pair 에서 ``[exit code: N]`` 정규식 추출. 없으면 None.
- ``lint_errors``: 마지막 reviewer ``ToolMessage`` 본문에서 정수 추출 (best-effort).
                   파싱 실패 시 None — "신호 없음" 으로 처리.
- ``prd_coverage``: working_directory 의 PRD 파일 (있을 때) 의 명사구가 산출
                   디렉토리 텍스트에 얼마나 등장하는지 비율. PRD 부재 시 1.0.

신호 누락은 항상 보수적으로 (1.0 / None) — rule_gate 가 잘못 LOW 판정해
불필요한 retry 를 트리거하지 않도록 한다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from langchain_core.messages import ToolMessage

if TYPE_CHECKING:
    from coding_agent.tools.todo_tool import TodoStore

log = structlog.get_logger("sufficiency.signals")


# verifier ToolMessage 본문은 task_tool._format_verifier_output 가 만든다 —
# "- command: <cmd>" 다음에 "result:" 블록이 따라오고 거기에 [exit code: N]
# 같은 marker 가 들어간다. 마커가 한 개도 없으면 모든 execute 가 성공한 것.
_EXIT_CODE_RE = re.compile(r"\[exit code:\s*(-?\d+)\]")
_TIMEOUT_MARKER = "[TIMEOUT]"
_REJECTED_MARKER = "REJECTED:"

# reviewer 가 lint 결과를 자유 형식으로 보고 — "X errors", "lint errors: N"
# 같은 패턴을 best-effort 로 잡는다. 실패 시 None.
_LINT_COUNT_RE = re.compile(
    r"(?:lint(?:ing)?\s+errors?|errors?\s+found|총\s*오류|오류\s*\d+)"
    r"\s*[:=]?\s*(\d+)",
    re.IGNORECASE,
)

# PRD 파일 후보 (working_directory 에 있을 때) — planner 가 보통 이 위치에
# 분해 산출물을 둔다. 첫 매치 우선.
_PRD_CANDIDATES = (
    "PRD.md",
    "prd.md",
    "docs/PRD.md",
    "docs/prd.md",
    "integrated_tasks.md",
    "tasks.md",
)

# PRD 에서 키워드로 추출할 명사구 — 한국어/영어 헤더 라인과 bullet 항목
_PRD_KEYWORD_RE = re.compile(
    r"^[\s\-\*\#0-9\.]*([A-Za-z가-힣][\w가-힣\s\-/]{2,40})", re.MULTILINE
)


def _last_tool_message_for(messages: list[Any], tool_name: str) -> ToolMessage | None:
    """Find the most recent ToolMessage whose ``name`` matches ``tool_name``.

    task_tool 이 SubAgent 결과를 ToolMessage 로 돌려주면서 ``name="task"``
    로 라벨링하지만 본문 첫 줄이 ``[Task COMPLETED — verifier]`` 형태로
    시작하므로 그것으로 role 식별.
    """
    role_marker = f"— {tool_name}"
    for m in reversed(messages):
        if not isinstance(m, ToolMessage):
            continue
        content = m.content if isinstance(m.content, str) else ""
        if role_marker in content[:80]:
            return m
    return None


def _extract_pytest_exit(messages: list[Any]) -> int | None:
    """verifier ToolMessage 에서 가장 최근 execute 호출의 exit code 추출.

    여러 execute 호출이 있으면 가장 *큰* exit code 를 반환 (실패 신호 우선).
    timeout / rejected 마커가 발견되면 -1. 마커가 하나도 없으면 0.
    verifier 가 아예 없거나 execute 가 없으면 None.
    """
    msg = _last_tool_message_for(messages, "verifier")
    if msg is None:
        return None
    content = msg.content if isinstance(msg.content, str) else ""
    if "execute(command, result) pairs" not in content:
        return None
    exits = [int(m.group(1)) for m in _EXIT_CODE_RE.finditer(content)]
    has_timeout = _TIMEOUT_MARKER in content
    has_rejected = _REJECTED_MARKER in content
    if has_timeout or has_rejected:
        return -1
    if exits:
        return max(exits, key=abs)
    return 0


def _extract_lint_errors(messages: list[Any]) -> int | None:
    """reviewer ToolMessage 본문에서 lint 오류 숫자 추출 (best-effort).

    파싱 실패 / reviewer 부재 시 None — rule_gate 는 None 을 "신호 없음"
    (제약 없음) 으로 다룬다.
    """
    msg = _last_tool_message_for(messages, "reviewer")
    if msg is None:
        return None
    content = msg.content if isinstance(msg.content, str) else ""
    m = _LINT_COUNT_RE.search(content)
    if m is None:
        return None
    try:
        return int(m.group(1))
    except (ValueError, IndexError):
        return None


def _read_prd(working_directory: str | None) -> tuple[str | None, str | None]:
    """Returns (prd_text, prd_relpath). Both None when no PRD file exists."""
    if not working_directory:
        return None, None
    base = Path(working_directory)
    if not base.exists():
        return None, None
    for candidate in _PRD_CANDIDATES:
        path = base / candidate
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8", errors="replace"), candidate
            except Exception as exc:  # noqa: BLE001
                log.debug("sufficiency.prd.read_failed", path=str(path), error=str(exc))
                continue
    return None, None


def _extract_prd_keywords(prd_text: str) -> list[str]:
    """Extract candidate noun-phrases from the PRD text.

    Heuristic only — picks lines that look like headings or bullets and
    keeps the first 2-40 character word run. Duplicates dropped.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in _PRD_KEYWORD_RE.finditer(prd_text):
        kw = m.group(1).strip()
        if len(kw) < 3 or kw.lower() in {"todo", "task", "summary"}:
            continue
        norm = kw.lower()
        if norm in seen:
            continue
        seen.add(norm)
        out.append(kw)
        if len(out) >= 40:
            break
    return out


def _scan_workspace_text(
    working_directory: str | None,
    *,
    skip_paths: set[str] | None = None,
) -> str:
    """Aggregate text from common output locations for keyword matching.

    Walks the working directory but skips heavy / vendor dirs and the
    PRD source files (``skip_paths``) themselves — including the PRD
    in the haystack would auto-match every keyword and inflate coverage
    to 1.0 regardless of actual implementation. Capped at ~512 KB to
    keep matching cheap. Includes file *paths* so keywords like "Order"
    can match ``backend/src/order/...`` even if the file content uses
    English identifiers.
    """
    if not working_directory:
        return ""
    base = Path(working_directory)
    if not base.exists():
        return ""
    skip_dirs = {
        ".git", ".ax-agent", "node_modules", "__pycache__", ".venv",
        "venv", "dist", "build", ".pytest_cache", "memory_store",
    }
    skip_paths = skip_paths or set()
    parts: list[str] = []
    total = 0
    cap = 512 * 1024
    for path in base.rglob("*"):
        if any(p in skip_dirs for p in path.parts):
            continue
        if not path.is_file():
            continue
        try:
            rel = str(path.relative_to(base))
        except ValueError:
            rel = str(path)
        if rel in skip_paths:
            continue
        if path.stat().st_size > 64 * 1024:
            continue
        if path.suffix.lower() not in {
            ".md", ".txt", ".py", ".ts", ".tsx", ".js", ".jsx",
            ".json", ".yaml", ".yml", ".toml", ".prisma",
        }:
            # Path 만이라도 포함 — 키워드가 디렉토리 명에서만 잡혀도 OK.
            parts.append(str(path.relative_to(base)))
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            parts.append(str(path.relative_to(base)))
            continue
        parts.append(str(path.relative_to(base)))
        parts.append(text)
        total += len(text)
        if total > cap:
            break
    return "\n".join(parts)


def _compute_prd_coverage(working_directory: str | None) -> float:
    """0.0~1.0 — PRD 의 키워드가 워크스페이스 산출물 텍스트에 등장한 비율.

    PRD 가 없으면 1.0 (= 신호 없음). 키워드가 추출되지 않으면 1.0.
    """
    prd_text, prd_path = _read_prd(working_directory)
    if not prd_text:
        return 1.0
    keywords = _extract_prd_keywords(prd_text)
    if not keywords:
        return 1.0
    skip = {prd_path} if prd_path else None
    haystack = _scan_workspace_text(working_directory, skip_paths=skip).lower()
    if not haystack:
        return 0.0
    hits = sum(1 for kw in keywords if kw.lower() in haystack)
    return hits / len(keywords)


def collect_signals(
    state: dict[str, Any],
    todo_store: "TodoStore | None",
) -> dict[str, Any]:
    """sufficiency rule_gate 가 소비할 신호 dict 를 반환.

    누락된 신호는 None / 1.0 등 "보수적" 기본값을 채워 LOW 오판을
    줄인다. rules.evaluate 가 None 을 어떻게 다루는지 결정한다.
    """
    messages = state.get("messages", []) or []
    working_directory = state.get("working_directory")

    # todo
    if todo_store is not None:
        try:
            counts = todo_store.counts()
        except Exception as exc:  # noqa: BLE001
            log.debug("sufficiency.todo_counts_failed", error=str(exc))
            counts = {}
    else:
        counts = {}
    todo_done = int(counts.get("completed", 0))
    todo_total = sum(int(v) for v in counts.values())
    todo_ratio = (todo_done / todo_total) if todo_total > 0 else 1.0

    return {
        "pytest_exit": _extract_pytest_exit(messages),
        "lint_errors": _extract_lint_errors(messages),
        "todo_done": todo_done,
        "todo_total": todo_total,
        "todo_ratio": todo_ratio,
        "prd_coverage": _compute_prd_coverage(working_directory),
    }


__all__ = ["collect_signals"]
