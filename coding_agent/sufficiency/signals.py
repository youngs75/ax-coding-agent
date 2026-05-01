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


_ARTIFACT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # (artifact_id, search keywords) — 사용자 요청 텍스트에서 키워드 매칭 시
    # 그 산출물이 *기대됨* 으로 표시. 워크스페이스에서 실제 파일/디렉토리
    # 존재를 별도 확인.
    ("prd", ("prd", "product requirements", "요구사항 문서", "요구사항 정의")),
    ("spec", ("spec", "specification", "명세서", "spec driven", "spec-driven", "sdd")),
    ("ledger", ("분해", "원자 단위", "atomic", "task 분해", "task breakdown", "task list", "작업 목록", "작업 분해", "wbs")),
)

_ARTIFACT_FILES: dict[str, tuple[str, ...]] = {
    "prd": (
        "PRD.md", "prd.md", "PRD.txt", "prd.txt",
        "docs/PRD.md", "docs/prd.md",
        "PMS_PRD.md", "pms_prd.md",
        "requirements.md", "Requirements.md",
    ),
    "spec": (
        "SPEC.md", "spec.md", "Specification.md", "specification.md",
        "docs/SPEC.md", "docs/spec.md", "docs/specification.md",
        "PMS_SPEC.md", "pms_spec.md",
        "design.md", "Design.md",
    ),
    # ledger 는 파일이 아니라 todo_store 의 상태로 확인 — 별도 처리.
}

# v22 #3 — DONE_CONDITION.md 후보 경로. planner 가 작성하면 sufficiency 가
# 워크스페이스 산출물과 *기계적*으로 대조한다.
_DONE_CONDITION_CANDIDATES: tuple[str, ...] = (
    "DONE_CONDITION.md",
    "done_condition.md",
    "docs/DONE_CONDITION.md",
    "docs/done_condition.md",
)

# DONE_CONDITION.md 포맷 — 헤더 다음 bullet 패턴으로 forbidden glob 들을
# 추출. 자유 형식 markdown 안에서 ``## Forbidden Patterns`` 섹션을 찾고
# 그 아래 ``- *.ext`` / ``- pattern`` 으로 시작하는 줄을 모은다.
_FORBIDDEN_HEADER_RE = re.compile(
    r"^##\s+Forbidden\s+Patterns\b", re.IGNORECASE | re.MULTILINE
)
# 다음 ## 헤더 또는 EOF 까지를 섹션 끝으로 본다.
_NEXT_H2_RE = re.compile(r"^##\s+", re.MULTILINE)
_BULLET_PATTERN_RE = re.compile(r"^\s*[-*]\s+([^\s(]+)", re.MULTILINE)

# v22.4 — Forbidden Patterns bullet 에 자연어 조건어가 섞이면 그 패턴은
# 무효화. v25 회귀 — planner 가 ``**/requirements.txt must exist`` 같은
# *반대 의미* bullet 작성 → harness 가 그대로 채택해 false-positive 위반
# 폭주. 안전망: bullet line 의 *괄호 밖* 텍스트에 키워드가 보이면 거부.
# 괄호 안 메모 (e.g., ``(React was chosen)``) 는 자유 텍스트로 허용.
_NL_CONDITION_RE = re.compile(
    r"\b(?:must|should|shall|if|when|unless|only|except|cannot|can\s*not|"
    r"will|need(?:ed|s)?)\b"
    r"|필수|있어야|없어야|해야|하는\s*경우|할\s*때",
    re.IGNORECASE,
)
_PAREN_NOTE_RE = re.compile(r"\([^)]*\)")


def _user_request_text(messages: list) -> str:
    """첫 HumanMessage 의 content. 사용자 의도 추출용 단일 진입점."""
    from langchain_core.messages import HumanMessage as _HM
    for m in messages:
        if isinstance(m, _HM):
            content = m.content if isinstance(m.content, str) else ""
            return content
    return ""


def _detect_artifact_intent(user_request: str) -> set[str]:
    """사용자 요청에서 *기대되는 산출물* 식별자 집합."""
    if not user_request:
        return set()
    text = user_request.lower()
    intent: set[str] = set()
    for artifact_id, keywords in _ARTIFACT_KEYWORDS:
        for kw in keywords:
            if kw.lower() in text:
                intent.add(artifact_id)
                break
    return intent


def _check_artifacts_present(
    working_directory: str | None,
    intent: set[str],
    todo_total: int,
) -> set[str]:
    """``intent`` 중 *실제로 워크스페이스에 존재* 가 확인된 항목.

    파일 기반 산출물(prd/spec)은 ``_ARTIFACT_FILES`` 의 후보 경로 중 하나
    라도 존재하면 충족. ``ledger`` 는 todo_total > 0 이면 충족.
    """
    present: set[str] = set()
    if "ledger" in intent and todo_total > 0:
        present.add("ledger")

    if not working_directory:
        return present
    base = Path(working_directory)
    if not base.exists():
        return present

    for artifact_id in intent:
        if artifact_id == "ledger":
            continue
        candidates = _ARTIFACT_FILES.get(artifact_id, ())
        for cand in candidates:
            if (base / cand).is_file():
                present.add(artifact_id)
                break
    return present


def _read_done_condition(working_directory: str | None) -> str | None:
    """v22 #3 — DONE_CONDITION.md 본문 반환 (없으면 None)."""
    if not working_directory:
        return None
    base = Path(working_directory)
    if not base.exists():
        return None
    for candidate in _DONE_CONDITION_CANDIDATES:
        path = base / candidate
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
    return None


def _extract_forbidden_patterns(done_condition_text: str) -> list[str]:
    """``## Forbidden Patterns`` 섹션에서 bullet 패턴 추출.

    예:
    ```
    ## Forbidden Patterns
    - *.vue (React was chosen)
    - *.svelte
    ```
    → ``["*.vue", "*.svelte"]``

    v22.4 — bullet 라인의 *괄호 밖* 텍스트에 자연어 조건어 (must/should/if/
    필수/있어야 등) 가 보이면 그 패턴은 *무효화*. planner skill 가이드
    (``done-condition.md``) 가 동일 규칙을 명시하지만 이 함수가 *deterministic
    안전망* 으로 회귀 차단.
    """
    m = _FORBIDDEN_HEADER_RE.search(done_condition_text)
    if not m:
        return []
    section_start = m.end()
    next_h = _NEXT_H2_RE.search(done_condition_text, section_start)
    section_end = next_h.start() if next_h else len(done_condition_text)
    section = done_condition_text[section_start:section_end]

    patterns: list[str] = []
    for line in section.splitlines():
        bm = _BULLET_PATTERN_RE.match(line)
        if bm is None:
            continue
        pattern = bm.group(1).strip()
        # 괄호 안 메모는 검사 제외 — `(React was chosen)` 같은 자연스런
        # 영어 메모를 허용. 괄호 *밖* 에서만 자연어 키워드 검사.
        outside = _PAREN_NOTE_RE.sub(" ", line)
        if _NL_CONDITION_RE.search(outside):
            log.debug(
                "sufficiency.forbidden_pattern_rejected_natural_language",
                line=line.strip(),
                pattern=pattern,
            )
            continue
        patterns.append(pattern)
    return patterns


def _detect_forbidden_violations(
    working_directory: str | None,
    forbidden_patterns: list[str],
) -> list[str]:
    """DONE_CONDITION 의 forbidden glob 패턴이 워크스페이스에 매치되면
    *위반* 으로 표시. 매치된 파일 경로를 ``"pattern → path"`` 형식으로 반환.

    skip_dirs: node_modules / .git / dist / build 등 vendor / artifact 경로는
    제외 (사용자 코드만 대상).
    """
    if not working_directory or not forbidden_patterns:
        return []
    base = Path(working_directory)
    if not base.exists():
        return []
    skip_dirs = {
        ".git", ".ax-agent", "node_modules", "__pycache__", ".venv",
        "venv", "dist", "build", ".pytest_cache", "memory_store",
        ".pnpm-store", ".turbo", ".next",
    }
    violations: list[str] = []
    for pattern in forbidden_patterns:
        for path in base.rglob(pattern):
            if any(p in skip_dirs for p in path.parts):
                continue
            if not path.is_file():
                continue
            rel = str(path.relative_to(base))
            violations.append(f"{pattern} → {rel}")
            if len(violations) >= 20:  # 폭주 방지
                return violations
    return violations


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

    # ── 산출물 의도 vs 실제 (옵션 C 의 핵심) ──
    # 사용자가 "PRD 작성", "분해", "SPEC" 같은 산출물을 요청했는데 SubAgent
    # 가 ask 만 하고 종료 (deepseek 패턴) 한 케이스를 *결과 검증* 으로 잡음.
    # SubAgent 가 COMPLETED 라고 주장해도 산출물이 없으면 sufficiency 가
    # LOW band 로 분류해 planner replan 자동 트리거.
    user_request = _user_request_text(messages)
    artifact_intent = _detect_artifact_intent(user_request)
    artifacts_present = _check_artifacts_present(
        working_directory, artifact_intent, todo_total
    )
    artifacts_missing = sorted(artifact_intent - artifacts_present)

    # v22 #3 — DONE_CONDITION.md 기반 결정론 게이트.
    # planner 가 작성한 DONE_CONDITION.md 의 forbidden patterns 가 실제
    # 워크스페이스에 등장하면 stack misalignment 등 *기획 위반* 으로 LOW
    # 분류. v21 의 React 선택 → Vue 컴포넌트 작성 회귀 직접 차단.
    done_condition_text = _read_done_condition(working_directory)
    if done_condition_text:
        forbidden_patterns = _extract_forbidden_patterns(done_condition_text)
        done_condition_violations = _detect_forbidden_violations(
            working_directory, forbidden_patterns
        )
    else:
        done_condition_violations = []

    return {
        "artifact_intent": sorted(artifact_intent),
        "artifacts_missing": artifacts_missing,
        "pytest_exit": _extract_pytest_exit(messages),
        "lint_errors": _extract_lint_errors(messages),
        "todo_done": todo_done,
        "todo_total": todo_total,
        "todo_ratio": todo_ratio,
        "prd_coverage": _compute_prd_coverage(working_directory),
        "done_condition_violations": done_condition_violations,
    }


__all__ = ["collect_signals"]
