"""Structured SPEC submission tool — harness-level output quality enforcement.

Motivation
----------
Weaker models (Qwen3, GLM-5) tend to produce partial SPEC documents: DB
schema only, no atomic task list, missing DoD. Prompt-level nudges do not
reliably fix this because the model still gets to decide on a single write.

Pattern borrowed from
  - Claude Code TodoWriteTool  (strict Zod schema, harness owns canonical state)
  - Codex update_plan          (section-by-section submission)
  - DeepAgents write_todos     (multi-stage pipeline instead of one big output)

Flow
----
1. Planner SubAgent calls ``submit_spec_section(section, content)``.
2. Pydantic validators enforce per-section rules. A failure returns a
   readable error to the LLM so it can retry the same section.
3. An in-process ``SpecSectionStore`` remembers which sections landed.
4. When all four sections are submitted, the harness assembles them into
   a deterministic ``docs/SPEC.md`` and returns a success message.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

SpecSectionName = Literal["goals", "tasks", "dependencies", "dod"]

_SECTION_ORDER: tuple[SpecSectionName, ...] = ("goals", "tasks", "dependencies", "dod")

_SECTION_TITLES: dict[SpecSectionName, str] = {
    "goals": "## 1. Goals",
    "tasks": "## 2. Atomic Tasks",
    "dependencies": "## 3. Dependencies",
    "dod": "## 4. Definition of Done",
}

# Validation thresholds — tuned so "minimum acceptable" equals "decent"
# rather than "barely passes". The planner must produce real detail per
# task (artifact paths + acceptance criteria) not just one-line bullets.
_MIN_GOALS_LEN = 200
_MIN_GOALS_BULLETS = 3

_MIN_TASKS_LEN = 1200  # total section length — forces per-task detail
_MIN_TASK_COUNT = 10   # atomic task count (was 8)
_TASK_ID_PATTERN = re.compile(r"TASK-\d{2,}")

# Per-task structural requirements: each TASK-NN block must contain
# BOTH an artifact/file reference AND an acceptance criterion marker.
_ARTIFACT_MARKERS = re.compile(
    r"("
    r"산출물|파일|경로|엔드포인트|artifact|file|path|endpoint|"
    r"\.(?:py|ts|tsx|js|jsx|vue|svelte|md|sql|yaml|yml|json|css|scss|go|rs|java)\b|"
    r"/[a-zA-Z0-9_./-]+"
    r")",
    re.IGNORECASE,
)
_ACCEPTANCE_MARKERS = re.compile(
    r"("
    r"GWT|Given.*When.*Then|Given/When/Then|"
    r"(?:^|\n)\s*[-*]?\s*G\s*:.*?(?:\n.*?)?\s*[-*]?\s*W\s*:.*?(?:\n.*?)?\s*[-*]?\s*T\s*:|"
    r"수용\s*기준|acceptance|"
    r"- \[[ xX]\]"  # checkbox counts as acceptance too
    r")",
    re.IGNORECASE | re.DOTALL,
)

_MIN_DEPENDENCY_EDGES = 3  # was 1
_DEPENDENCY_EDGE_PATTERN = re.compile(
    r"TASK-\d{2,}\s*(?:->|→|depends on|blocks)\s*TASK-\d{2,}",
    re.IGNORECASE,
)

_MIN_DOD_CHECKBOXES = 25  # was 20
_CHECKBOX_PATTERN = re.compile(r"^\s*-\s*\[[ xX]\]", re.MULTILINE)

_BULLET_PATTERN = re.compile(r"^\s*[-*]\s+\S", re.MULTILINE)


def _split_task_blocks(stripped: str) -> list[tuple[str, str]]:
    """Split a tasks section into (task_id, block_content) pairs.

    Each block runs from one TASK-NN marker up to (but not including)
    the next TASK-NN marker. If the section has no markers the list
    is empty.
    """
    blocks: list[tuple[str, str]] = []
    matches = list(_TASK_ID_PATTERN.finditer(stripped))
    for i, m in enumerate(matches):
        task_id = m.group(0)
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(stripped)
        block = stripped[start:end]
        blocks.append((task_id, block))
    return blocks


class SpecSectionInput(BaseModel):
    """Input schema for the ``submit_spec_section`` tool."""

    section: SpecSectionName = Field(
        description=(
            "Which SPEC section this content belongs to. One of: "
            "'goals', 'tasks', 'dependencies', 'dod'."
        ),
    )
    content: str = Field(
        description="The full markdown body for this section (no heading).",
        min_length=1,
    )

    # NOTE: per-section validation runs inside the tool function (not as a
    # Pydantic field_validator) so violations come back to the LLM as a
    # readable 'REJECTED (...)' tool result instead of a hard exception.


def validate_section_content(section: SpecSectionName, content: str) -> None:
    """Raise ``ValueError`` with a planner-friendly message if content is weak.

    Exposed separately so the tool and the store can share the same rules
    (and so tests can exercise the validators directly).
    """
    stripped = content.strip()

    if section == "goals":
        if len(stripped) < _MIN_GOALS_LEN:
            raise ValueError(
                f"'goals' must be at least {_MIN_GOALS_LEN} characters "
                f"(got {len(stripped)}). Describe the product outcome, "
                "target users, and success criteria."
            )
        bullet_count = len(_BULLET_PATTERN.findall(stripped))
        if bullet_count < _MIN_GOALS_BULLETS:
            raise ValueError(
                f"'goals' must contain at least {_MIN_GOALS_BULLETS} bullet "
                f"points (got {bullet_count}). Use '- ' or '* ' for each goal."
            )
        return

    if section == "tasks":
        if len(stripped) < _MIN_TASKS_LEN:
            raise ValueError(
                f"'tasks' must be at least {_MIN_TASKS_LEN} characters "
                f"(got {len(stripped)}). Each task needs artifact paths "
                "AND acceptance criteria (Given/When/Then or checklist) — "
                "one-line bullets are not enough."
            )
        task_ids = _TASK_ID_PATTERN.findall(stripped)
        unique_ids = set(task_ids)
        if len(unique_ids) < _MIN_TASK_COUNT:
            raise ValueError(
                f"'tasks' must define at least {_MIN_TASK_COUNT} atomic tasks "
                f"using IDs like TASK-01, TASK-02, ... "
                f"(found {len(unique_ids)} unique IDs)."
            )
        # ── Per-task structural validation ──
        # Each TASK-NN block must contain BOTH an artifact reference
        # (file path / module / endpoint) AND an acceptance criterion
        # (GWT / Given-When-Then / checklist / 수용 기준).
        blocks = _split_task_blocks(stripped)
        missing_artifact: list[str] = []
        missing_acceptance: list[str] = []
        too_short: list[str] = []
        for task_id, block in blocks:
            if len(block) < 100:
                too_short.append(task_id)
                continue
            if not _ARTIFACT_MARKERS.search(block):
                missing_artifact.append(task_id)
            if not _ACCEPTANCE_MARKERS.search(block):
                missing_acceptance.append(task_id)
        if too_short:
            raise ValueError(
                f"Tasks {', '.join(too_short[:5])} are too short (<100 chars). "
                "Each task block must include artifact paths AND acceptance criteria — "
                "write several lines per task, not a single bullet."
            )
        if missing_artifact:
            raise ValueError(
                f"Tasks {', '.join(missing_artifact[:5])} are missing artifact references "
                "(산출물/파일경로/endpoint). Example format per task:\n"
                "- TASK-01: 인증 API\n"
                "  - **산출물**: src/api/auth.py, tests/test_auth.py\n"
                "  - **GWT**\n"
                "    - G: 가입된 사용자가 존재\n"
                "    - W: POST /api/login {email, password}\n"
                "    - T: 200 {access_token}, 잘못된 비밀번호 시 401"
            )
        if missing_acceptance:
            raise ValueError(
                f"Tasks {', '.join(missing_acceptance[:5])} are missing acceptance "
                "criteria. Each task needs Given/When/Then (GWT) or a checklist. "
                "Example:\n"
                "- TASK-01: ...\n"
                "  - **산출물**: path/to/file.py\n"
                "  - **GWT**\n"
                "    - G: <초기 상태>\n"
                "    - W: <행위>\n"
                "    - T: <기대 결과>"
            )
        return

    if section == "dependencies":
        edges = _DEPENDENCY_EDGE_PATTERN.findall(stripped)
        if len(edges) < _MIN_DEPENDENCY_EDGES:
            raise ValueError(
                "'dependencies' must declare at least "
                f"{_MIN_DEPENDENCY_EDGES} dependency edge of the form "
                "'TASK-01 -> TASK-02' (or 'depends on' / 'blocks'). "
                f"Found {len(edges)}."
            )
        return

    if section == "dod":
        checkboxes = _CHECKBOX_PATTERN.findall(stripped)
        if len(checkboxes) < _MIN_DOD_CHECKBOXES:
            raise ValueError(
                f"'dod' must include at least {_MIN_DOD_CHECKBOXES} checklist "
                f"items using '- [ ]' syntax (found {len(checkboxes)}). "
                "Cover test, lint, build, deploy, and acceptance criteria."
            )
        return

    raise ValueError(f"Unknown section: {section!r}")


class SpecSectionStore:
    """Thread-safe in-process store for submitted SPEC sections.

    One store instance is bound to one planner SubAgent invocation via
    closure in :func:`build_submit_spec_section_tool`. Completed sections
    are auto-assembled into ``docs/SPEC.md`` when the set is complete.
    """

    def __init__(self, spec_path: Path | str = "docs/SPEC.md") -> None:
        self._sections: dict[SpecSectionName, str] = {}
        self._lock = threading.Lock()
        self._spec_path = Path(spec_path)
        self._assembled = False

    # ── Public API ─────────────────────────────────────────────

    def submit(self, section: SpecSectionName, content: str) -> str:
        """Record *content* under *section* and assemble SPEC.md if complete.

        Returns a human-readable status message suitable for returning to
        the LLM verbatim.
        """
        with self._lock:
            self._sections[section] = content.strip()
            submitted = sorted(self._sections.keys(), key=_SECTION_ORDER.index)
            remaining = [s for s in _SECTION_ORDER if s not in self._sections]

            if not remaining:
                if not self._assembled:
                    self._write_spec()
                    self._assembled = True
                return (
                    "ALL_SECTIONS_SUBMITTED: "
                    f"'{self._spec_path.as_posix()}' written. "
                    "Planner may now finish with the final summary."
                )

            return (
                f"OK — '{section}' accepted "
                f"({len(submitted)}/{len(_SECTION_ORDER)} sections). "
                f"Remaining: {', '.join(remaining)}."
            )

    def submitted_sections(self) -> list[SpecSectionName]:
        with self._lock:
            return [s for s in _SECTION_ORDER if s in self._sections]

    def is_complete(self) -> bool:
        with self._lock:
            return all(s in self._sections for s in _SECTION_ORDER)

    def assemble(self) -> str:
        """Return the assembled SPEC.md markdown without writing to disk."""
        with self._lock:
            return self._assemble_unlocked()

    # ── Internals ──────────────────────────────────────────────

    def _assemble_unlocked(self) -> str:
        parts: list[str] = ["# SPEC", ""]
        for section in _SECTION_ORDER:
            parts.append(_SECTION_TITLES[section])
            parts.append("")
            parts.append(self._sections.get(section, "_(missing)_"))
            parts.append("")
        return "\n".join(parts).rstrip() + "\n"

    def _write_spec(self) -> None:
        rendered = self._assemble_unlocked()
        target = self._spec_path
        if not target.is_absolute():
            target = Path.cwd() / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")


def build_submit_spec_section_tool(
    store: SpecSectionStore | None = None,
) -> StructuredTool:
    """Build a fresh ``submit_spec_section`` StructuredTool bound to *store*.

    Each planner SubAgent gets its own store so sections from different
    runs never leak into the same SPEC.md.
    """
    bound_store = store or SpecSectionStore()

    def _run(section: SpecSectionName, content: str) -> str:
        try:
            validate_section_content(section, content)
        except ValueError as exc:
            return f"REJECTED ({section}): {exc}"

        return bound_store.submit(section, content)

    tool = StructuredTool.from_function(
        func=_run,
        name="submit_spec_section",
        description=(
            "Submit one section of the SPEC document under a strict schema. "
            "Call this exactly 4 times — once per section — instead of "
            "write_file(docs/SPEC.md). Sections: 'goals', 'tasks', "
            "'dependencies', 'dod'. On the fourth call, the harness will "
            "assemble and write docs/SPEC.md for you. Validation errors "
            "come back as 'REJECTED (...)' — fix the content and resubmit."
        ),
        args_schema=SpecSectionInput,
    )
    # Attach the store so callers (manager/factory) can inspect / reset it.
    tool.metadata = {"spec_store": bound_store}  # type: ignore[attr-defined]
    return tool
