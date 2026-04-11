"""SubAgentFactory — creates SubAgent instances with role-based templates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from langchain_openai import ChatOpenAI

from coding_agent.models import get_model
from coding_agent.subagents.models import SubAgentInstance
from coding_agent.subagents.registry import SubAgentRegistry

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _RoleTemplate:
    """Blueprint for a SubAgent role."""

    system_prompt_template: str
    default_tools: list[str]
    model_tier: str


# ── Role templates ────────────────────────────────────────────

_PLANNER_PROMPT = """\
You are a planning agent. Your job is to analyze the task, explore the codebase,
and produce clear, actionable plans and documents.

Task: {task_summary}

Guidelines:
- Read relevant files to understand the current architecture.
- Identify affected modules, interfaces, and tests.
- Output a numbered step-by-step plan. Be specific about file paths and changes.
- Use write_file to save planning documents (PRD, SPEC, etc.) to the requested path.
- Do NOT write application code — only plans and specification documents.
"""

_CODER_PROMPT = """\
You are a coding agent. Your job is to implement the requested changes precisely.

Task: {task_summary}

Guidelines:
- Read existing files before modifying them.
- Write clean, production-quality code that follows existing conventions.
- Create or update tests when appropriate.
- After writing files, verify correctness with a quick execution if possible.
"""

_REVIEWER_PROMPT = """\
You are a code review agent. Your job is to review code changes for correctness,
style, and potential issues.

Task: {task_summary}

Guidelines:
- Read the relevant files and understand the context.
- Check for bugs, edge cases, and style violations.
- Provide a structured review with severity levels (critical, warning, info).
- Suggest specific fixes for any issues found.
"""

_FIXER_PROMPT = """\
You are a bug-fixing agent. Your job is to diagnose and fix the reported issue.

Task: {task_summary}

Guidelines:
- Reproduce the issue if possible (run tests or execute code).
- Read relevant source files and trace the root cause.
- Apply a minimal, targeted fix.
- Verify the fix works by re-running the failing test or command.
"""

_RESEARCHER_PROMPT = """\
You are a research agent. Your job is to gather information from the codebase
and summarize findings.

Task: {task_summary}

Guidelines:
- Search broadly using glob and grep to find relevant code.
- Read and understand the key files.
- Provide a concise summary with file paths and code references.
"""

ROLE_TEMPLATES: dict[str, _RoleTemplate] = {
    "planner": _RoleTemplate(
        system_prompt_template=_PLANNER_PROMPT,
        default_tools=["read_file", "write_file", "glob_files", "grep"],
        model_tier="reasoning",
    ),
    "coder": _RoleTemplate(
        system_prompt_template=_CODER_PROMPT,
        default_tools=["read_file", "write_file", "edit_file", "execute", "glob_files", "grep"],
        model_tier="strong",
    ),
    "reviewer": _RoleTemplate(
        system_prompt_template=_REVIEWER_PROMPT,
        default_tools=["read_file", "glob_files", "grep"],
        model_tier="default",
    ),
    "fixer": _RoleTemplate(
        system_prompt_template=_FIXER_PROMPT,
        default_tools=["read_file", "edit_file", "execute", "grep"],
        model_tier="strong",
    ),
    "researcher": _RoleTemplate(
        system_prompt_template=_RESEARCHER_PROMPT,
        default_tools=["read_file", "glob_files", "grep"],
        model_tier="default",
    ),
}

# Task-analysis prompt used by _analyze_task to classify into a role
_CLASSIFY_PROMPT = """\
You are a task classifier. Given a task description, determine the single best
agent role from the following list:

- planner: for tasks that require architecture planning, design decisions, or creating step-by-step plans
- coder: for tasks that require writing, generating, or implementing code
- reviewer: for tasks that require reviewing, auditing, or critiquing existing code
- fixer: for tasks that require debugging, fixing bugs, or resolving errors
- researcher: for tasks that require searching, reading, or gathering information

Respond with ONLY the role name (one word, lowercase). No explanation.

Task: {task_description}
"""


class SubAgentFactory:
    """Creates SubAgent instances with appropriate role configuration."""

    def __init__(self, registry: SubAgentRegistry, llm: ChatOpenAI) -> None:
        self._registry = registry
        self._llm = llm

    def create_for_task(
        self,
        task_description: str,
        parent_id: str | None = None,
        agent_type: str = "auto",
    ) -> SubAgentInstance:
        """Create a SubAgent instance suited for *task_description*.

        If *agent_type* is ``"auto"``, the factory uses a fast LLM call to
        classify the task into a role. Otherwise the specified role template
        is used directly.
        """
        if agent_type != "auto" and agent_type in ROLE_TEMPLATES:
            role = agent_type
        elif agent_type != "auto":
            log.warning(
                "subagent.factory.unknown_role",
                requested=agent_type,
                fallback="coder",
            )
            role = "coder"
        else:
            role = self._analyze_task(task_description)

        template = ROLE_TEMPLATES[role]

        instance = self._registry.create_instance(
            role=role,
            specialty=template.system_prompt_template.split("\n")[0].strip(),
            task_summary=task_description,
            parent_id=parent_id,
            model_tier=template.model_tier,
            tools=list(template.default_tools),
        )

        log.info(
            "subagent.factory.created",
            agent_id=instance.agent_id,
            role=role,
            model_tier=template.model_tier,
        )
        return instance

    # ── Internal helpers ──────────────────────────────────────

    # Keyword-based fast classification — avoids an LLM round-trip for
    # the vast majority of tasks.  Only falls through to LLM if no
    # keywords match.
    _ROLE_KEYWORDS: dict[str, list[str]] = {
        "planner": [
            "설계", "계획", "분석", "아키텍처", "PRD", "SPEC", "요구사항",
            "plan", "design", "architect", "analyze", "requirement",
        ],
        "coder": [
            "구현", "작성", "생성", "코드", "코딩", "만들", "설치",
            "implement", "create", "write", "code", "build", "install", "setup",
        ],
        "reviewer": [
            "리뷰", "검토", "검증", "확인", "review", "audit", "check", "verify",
        ],
        "fixer": [
            "수정", "fix", "bug", "오류", "에러", "디버그", "debug", "repair", "실패",
        ],
        "researcher": [
            "조사", "탐색", "찾", "search", "research", "find", "explore",
        ],
    }

    def _analyze_task(self, task_description: str) -> str:
        """Classify task into a role — keyword match first, LLM only as fallback."""
        import time as _time
        t0 = _time.monotonic()
        desc_lower = task_description.lower()

        # Fast path: keyword matching (0ms, no API call)
        scores: dict[str, int] = {}
        for role, keywords in self._ROLE_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in desc_lower)
            if score > 0:
                scores[role] = score

        if scores:
            role = max(scores, key=scores.get)  # type: ignore[arg-type]
            log.info(
                "timing.classify_fast",
                task=task_description[:80],
                role=role,
                score=scores[role],
                elapsed_s=round(_time.monotonic() - t0, 4),
            )
            return role

        # Slow path: LLM classification (only when keywords don't match)
        try:
            fast_llm = get_model("fast", temperature=0.0)
            prompt = _CLASSIFY_PROMPT.format(task_description=task_description)
            response = fast_llm.invoke(prompt)
            role = response.content.strip().lower().split()[0] if response.content else "coder"

            if role not in ROLE_TEMPLATES:
                log.warning(
                    "subagent.factory.classify_fallback",
                    raw_response=role,
                    fallback="coder",
                )
                role = "coder"

            log.info(
                "timing.classify_llm",
                task=task_description[:80],
                role=role,
                elapsed_s=round(_time.monotonic() - t0, 3),
            )
            return role
        except Exception as exc:
            log.error("subagent.factory.classify_error", error=str(exc), fallback="coder")
            return "coder"

    @staticmethod
    def build_system_prompt(instance: SubAgentInstance) -> str:
        """Generate the full system prompt for an instance from its role template."""
        template = ROLE_TEMPLATES.get(instance.role)
        if template is None:
            # Fallback: generic prompt
            return (
                f"You are a helpful coding agent.\n\nTask: {instance.task_summary}\n\n"
                "Complete the task using the tools available to you."
            )
        return template.system_prompt_template.format(task_summary=instance.task_summary)
