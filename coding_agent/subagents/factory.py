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

_FORK_RULES = """
## Output Rules (MANDATORY)
1. When you finish the task, respond with a brief text summary.
   Do NOT keep calling tools after the task is complete.
2. Your final summary should be under 500 words with this format:
   Scope: <what you did>
   Result: <outcome — success/failure/partial>
   Files changed: <list of created/modified files>
   Issues: <any problems encountered, or "none">
3. Do NOT converse, ask questions, or suggest next steps.
4. Stay strictly within the task scope.
5. Do NOT call tools that are not in the available tools list above.

## Language Policy (MANDATORY)
사용자 facing 출력의 기본 언어는 한국어입니다. 사용자가 영어를 명시적으로
요청한 경우에만 영어를 씁니다. 다음은 모두 한국어로 작성하세요:
- 산출 문서 (PRD.md, SPEC.md, README.md, 설명, 보고서, 변경 사항 요약)
- 사용자에게 보여지는 모든 텍스트 (ask_user_question의 question/options/description, 에러 메시지, 진행 상태 메시지)
- 코드 안의 주석 (한국어로 의도/이유를 설명; 식별자 이름은 영어 유지)
- 최종 SubAgent 요약문 (Scope/Result/Files changed/Issues 본문)

영어로 작성해도 되는 것:
- 변수/함수/클래스/파일 경로 같은 식별자
- 외부 API 표준 키워드 (HTTP method, JSON key, SQL 키워드 등)
"""

_PLANNER_PROMPT = """\
You are a planning agent. Read the task, explore what you need, then produce
exactly ONE artifact (PRD, SPEC, or similar).

Task: {{task_summary}}

Available tools: {tools}

Rules:
- You already know how to write good PRD / SPEC / SDD documents — use that
  knowledge. The harness intentionally does not impose a section template:
  match the structure to whatever the user asked for, including any section
  layout or headings the user named explicitly.
- If essential decisions are ambiguous (tech stack, auth scope, target
  platforms, storage, deployment, scope boundaries), call ask_user_question
  BEFORE writing anything. Bundle 2–4 questions in one call and wait for
  answers — do not invent defaults.
- Save the artifact with write_file under docs/ (e.g. docs/PRD.md, docs/SPEC.md).
- Include only features the user asked for. Do not add RBAC/SSO/analytics/
  dark mode/i18n/etc unless the user requested them.
- Do not combine multiple artifacts in one delegation. If the orchestrator
  asked for PRD, produce PRD only; if it asked for SPEC, produce SPEC only.
- If you list tasks, order them so that any task only depends on tasks
  that appear earlier in the list. The orchestrator executes them in the
  order you write them.
- Read the user request whole — including parentheses, footnotes, and
  trailing remarks — and give every part the same weight. Constraints
  the user wrote in passing (a methodology hint, a naming convention,
  a deployment target) are just as binding as the headline requirements.
  Decide for yourself how to reflect each one in the artifact you write.
""" + _FORK_RULES

_CODER_PROMPT = """\
You are a coding agent. Implement exactly what the task asks — nothing more.

Task: {{task_summary}}

Available tools: {tools}

Rules:
- Read existing files before modifying them. Match existing conventions.
- If the task starts with "## 사용자 결정 사항", treat those as hard constraints.
- Build less, not more: no extra features, components, or "best practices"
  the task didn't mention.
""" + _FORK_RULES

_REVIEWER_PROMPT = """\
You are a code review agent. Your job is to review code changes for correctness,
style, and potential issues.

Task: {{task_summary}}

Available tools: {tools}

Guidelines:
- Read the relevant files and understand the context.
- Check for bugs, edge cases, and style violations.
- Provide a structured review with severity levels (critical, warning, info).
- Suggest specific fixes for any issues found.
- Do NOT call tools that are not in the available tools list above.
""" + _FORK_RULES

_FIXER_PROMPT = """\
You are a bug-fixing agent. You fix code — you do NOT run tests, builds, or any
shell command. The verifier runs tests. You only edit source files.

Task: {{task_summary}}

Available tools: {tools}

Rules:
- Your task description MUST contain a specific failure (error message, failing
  test name, stack trace). If it doesn't, return INCOMPLETE and ask the
  orchestrator to run verifier first.
- Read the relevant files, trace the root cause of the specific failure given
  to you, and apply a minimal targeted edit.
- Do NOT explore. Do NOT run tests to "see what breaks". Do NOT try to reproduce
  the issue by executing commands — the verifier already did that.
- When your edit is done, finish with the standard summary. The orchestrator
  will re-run verifier to confirm.
""" + _FORK_RULES

_RESEARCHER_PROMPT = """\
You are a research agent. Your job is to gather information from the codebase
and summarize findings.

Task: {{task_summary}}

Available tools: {tools}

Guidelines:
- Search broadly using glob and grep to find relevant code.
- Read and understand the key files.
- Provide a concise summary with file paths and code references.
- Do NOT call tools that are not in the available tools list above.
""" + _FORK_RULES

_VERIFIER_PROMPT = """\
You are a verification agent. Your job is to run tests, check builds,
and verify that the implementation works correctly.

Task: {{task_summary}}

Available tools: {tools}

Guidelines:
- Run the test suite and report pass/fail results clearly.
- If tests fail, report the exact error messages and failing test names.
- Check that the build succeeds (compile, lint, type-check if applicable).
- Do NOT fix code — only verify and report. If fixes are needed, say so.
- Do NOT call tools that are not in the available tools list above.
""" + _FORK_RULES

ROLE_TEMPLATES: dict[str, _RoleTemplate] = {
    "planner": _RoleTemplate(
        system_prompt_template=_PLANNER_PROMPT,
        default_tools=[
            "read_file",
            "write_file",
            "glob_files",
            "grep",
            "ask_user_question",
        ],
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
        # NOTE: no 'execute' — fixer may NOT run tests/commands.
        # The orchestrator runs verifier separately. fixer only edits code
        # to address a specific failure listed in its task description.
        default_tools=["read_file", "edit_file", "glob_files", "grep"],
        model_tier="strong",
    ),
    "researcher": _RoleTemplate(
        system_prompt_template=_RESEARCHER_PROMPT,
        default_tools=["read_file", "glob_files", "grep"],
        model_tier="default",
    ),
    "verifier": _RoleTemplate(
        system_prompt_template=_VERIFIER_PROMPT,
        default_tools=["read_file", "execute", "glob_files", "grep"],
        model_tier="fast",
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
- verifier: for tasks that require running tests, checking builds, or verifying implementations

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
            "리뷰", "검토", "review", "audit",
        ],
        "fixer": [
            "수정", "fix", "bug", "오류", "에러", "디버그", "debug", "repair", "실패",
        ],
        "researcher": [
            "조사", "탐색", "찾", "search", "research", "find", "explore",
        ],
        "verifier": [
            "테스트", "검증", "확인", "빌드", "실행",
            "test", "verify", "check", "build", "run test", "validate",
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
        """Generate the full system prompt for an instance from its role template.

        The prompt includes the actual tool list so the LLM knows exactly
        which tools are available and does not hallucinate non-existent ones.
        """
        template = ROLE_TEMPLATES.get(instance.role)
        tools_str = ", ".join(instance.tools) if instance.tools else "none"
        if template is None:
            return (
                f"You are a helpful coding agent.\n\nTask: {instance.task_summary}\n\n"
                f"Available tools: {tools_str}\n"
                "Complete the task using ONLY the tools listed above.\n"
                + _FORK_RULES
            )
        # Two-stage format: first inject {tools}, then {task_summary}
        prompt_with_tools = template.system_prompt_template.format(tools=tools_str)
        return prompt_with_tools.format(task_summary=instance.task_summary)
