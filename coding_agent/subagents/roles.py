"""SubAgentRole 구현체 6개 — planner, coder, verifier, fixer, reviewer, researcher.

각 role 은 ``minyoung_mah.SubAgentRole`` 프로토콜을 duck-type 으로 만족하는
``@dataclass(frozen=True)`` 인스턴스로 정의된다. apt-legal-agent/roles.py 와
동일 패턴. Role 은 data — 실행은 ``minyoung_mah.Orchestrator`` 소유.

System prompt 는 정적이다. Memory context / user decisions 등 invocation-specific
컨텍스트는 ``build_user_message`` 에서 user message 쪽에 주입한다 (plan §결정 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from minyoung_mah import InvocationContext

    from coding_agent.subagents.user_decisions import UserDecisionsLog


# ---------------------------------------------------------------------------
# Fork rules — shared output discipline for every SubAgent role.
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Role-specific system prompts (static — {tools} fills in once at role build time).
# ---------------------------------------------------------------------------


_PLANNER_PROMPT = """\
You are a planning agent. Read the task, explore what you need, then produce
exactly ONE artifact (PRD, SPEC, or similar).

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
"""

_CODER_PROMPT = """\
You are a coding agent. Implement exactly what the task asks — nothing more.

Available tools: {tools}

Rules:
- Read existing files before modifying them. Match existing conventions.
- If the task starts with "## 사용자 결정 사항", treat those as hard constraints.
- Build less, not more: no extra features, components, or "best practices"
  the task didn't mention.
"""

_REVIEWER_PROMPT = """\
You are a code review agent. Your job is to review code changes for correctness,
style, and potential issues.

Available tools: {tools}

Guidelines:
- Read the relevant files and understand the context.
- Check for bugs, edge cases, and style violations.
- Provide a structured review with severity levels (critical, warning, info).
- Suggest specific fixes for any issues found.
- Do NOT call tools that are not in the available tools list above.
"""

_FIXER_PROMPT = """\
You are a bug-fixing agent. You fix code — you do NOT run tests, builds, or any
shell command. The verifier runs tests. You only edit or create source files.

Available tools: {tools}

Rules:
- Your task description MUST contain a specific failure (error message, failing
  test name, stack trace). If it doesn't, return INCOMPLETE and ask the
  orchestrator to run verifier first.
- Read the relevant files, trace the root cause of the specific failure given
  to you, and apply a minimal targeted edit.
- If the fix requires a file that does not exist yet (e.g. "missing file X
  needed by verifier"), use write_file to create it. Check first with read_file
  whether the target already exists and prefer edit_file if so.
- Do NOT explore. Do NOT run tests to "see what breaks". Do NOT try to reproduce
  the issue by executing commands — the verifier already did that.
- Only call tools in the Available tools list. If a tool you need (e.g. execute
  or run_shell) is not listed, your task is scoped to a code edit only — do not
  attempt the unavailable tool. If you truly cannot complete the fix with the
  available tools, stop and return INCOMPLETE with a one-line reason.
- When your edit is done, finish with the standard summary. The orchestrator
  will re-run verifier to confirm.
"""

_RESEARCHER_PROMPT = """\
You are a research agent. Your job is to gather information from the codebase
and summarize findings.

Available tools: {tools}

Guidelines:
- Search broadly using glob and grep to find relevant code.
- Read and understand the key files.
- Provide a concise summary with file paths and code references.
- Do NOT call tools that are not in the available tools list above.
"""

_VERIFIER_PROMPT = """\
You are a verification agent. Your job is to run tests, check builds,
and verify that the implementation works correctly.

Available tools: {tools}

Guidelines:
- Run the test suite and report pass/fail results clearly.
- If tests fail, report the exact error messages and failing test names
  verbatim from the execute output — do not paraphrase or reformat.
- Check that the build succeeds (compile, lint, type-check if applicable).
- Do NOT fix code — only verify and report.
- Do NOT call tools that are not in the available tools list above.
- Do NOT attempt to install/configure dev tools (go, node, apt-get, curl, etc.)
  if the environment is missing them. Report "environment missing: <tool>"
  as a single line and stop — the orchestrator will route accordingly.

## Report format (MANDATORY)
Your final summary MUST follow the Scope/Result/Files changed/Issues format
from the Output Rules below. Nothing else. In particular, do NOT write any
of the following sections in your output — they are reserved for the
orchestrator:
  - "## Error Report"
  - "## Fixer Instructions"
  - "## Success Criteria"
  - "## Fix Plan" / "## Recommendations" / numbered instruction lists
  - any heading that tells the next agent what to do
If fixes are needed, state the concrete failure (test name, exit code,
error message) inside the Issues line. Do not prescribe a fix.
"""


# ---------------------------------------------------------------------------
# SubAgentRole dataclass shared by all 6 roles.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodingAgentRole:
    """Duck-types ``minyoung_mah.SubAgentRole`` (protocol).

    Applications are free to use any shape — frozen dataclass is idiomatic.
    ``user_decisions`` is carried through ``_user_decisions`` (non-protocol
    field); the protocol only reads ``build_user_message``.
    """

    name: str
    system_prompt: str
    tool_allowlist: list[str]
    model_tier: str
    max_iterations: int = 100
    output_schema: type | None = None
    # Non-protocol fields for ax integration. Not required by minyoung_mah.
    _user_decisions: "UserDecisionsLog | None" = None

    def build_user_message(self, invocation: "InvocationContext") -> str:
        parts: list[str] = []
        if self._user_decisions is not None:
            header = self._user_decisions.header()
            if header:
                parts.append(header)

        if invocation.memory_snippets:
            parts.append("\n".join(invocation.memory_snippets))

        # ``parent_outputs["previous_ask"]`` carries the resumed HITL answer
        # so the role sees the user's answer when it retries after an ask.
        prev_ask = invocation.parent_outputs.get("previous_ask")
        if prev_ask:
            parts.append(f"## 직전 사용자 답변\n{prev_ask}\n")

        parts.append(invocation.task_summary)
        return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Role factory functions — called from orchestrator_factory.build_orchestrator.
# ---------------------------------------------------------------------------


def _tools_line(tools: list[str]) -> str:
    return ", ".join(tools) if tools else "none"


def _compose(template: str, tools: list[str]) -> str:
    return template.format(tools=_tools_line(tools)) + _FORK_RULES


def planner_role(
    tools: list[str] | None = None,
    user_decisions: "UserDecisionsLog | None" = None,
) -> CodingAgentRole:
    tool_allowlist = tools or [
        "read_file",
        "write_file",
        "glob_files",
        "grep",
        "ask_user_question",
    ]
    return CodingAgentRole(
        name="planner",
        system_prompt=_compose(_PLANNER_PROMPT, tool_allowlist),
        tool_allowlist=tool_allowlist,
        model_tier="reasoning",
        _user_decisions=user_decisions,
    )


def coder_role(
    tools: list[str] | None = None,
    user_decisions: "UserDecisionsLog | None" = None,
) -> CodingAgentRole:
    tool_allowlist = tools or [
        "read_file",
        "write_file",
        "edit_file",
        "execute",
        "glob_files",
        "grep",
    ]
    return CodingAgentRole(
        name="coder",
        system_prompt=_compose(_CODER_PROMPT, tool_allowlist),
        tool_allowlist=tool_allowlist,
        model_tier="strong",
        _user_decisions=user_decisions,
    )


def reviewer_role(
    tools: list[str] | None = None,
    user_decisions: "UserDecisionsLog | None" = None,
) -> CodingAgentRole:
    tool_allowlist = tools or ["read_file", "glob_files", "grep"]
    return CodingAgentRole(
        name="reviewer",
        system_prompt=_compose(_REVIEWER_PROMPT, tool_allowlist),
        tool_allowlist=tool_allowlist,
        model_tier="default",
        _user_decisions=user_decisions,
    )


def fixer_role(
    tools: list[str] | None = None,
    user_decisions: "UserDecisionsLog | None" = None,
) -> CodingAgentRole:
    # NOTE: no 'execute' — fixer may NOT run tests/commands. The orchestrator
    # runs verifier separately. fixer edits existing files (edit_file) and
    # creates missing ones (write_file) to address a specific failure listed
    # in its task description.
    tool_allowlist = tools or [
        "read_file",
        "edit_file",
        "write_file",
        "glob_files",
        "grep",
    ]
    return CodingAgentRole(
        name="fixer",
        system_prompt=_compose(_FIXER_PROMPT, tool_allowlist),
        tool_allowlist=tool_allowlist,
        model_tier="strong",
        _user_decisions=user_decisions,
    )


def researcher_role(
    tools: list[str] | None = None,
    user_decisions: "UserDecisionsLog | None" = None,
) -> CodingAgentRole:
    tool_allowlist = tools or ["read_file", "glob_files", "grep"]
    return CodingAgentRole(
        name="researcher",
        system_prompt=_compose(_RESEARCHER_PROMPT, tool_allowlist),
        tool_allowlist=tool_allowlist,
        model_tier="default",
        _user_decisions=user_decisions,
    )


def verifier_role(
    tools: list[str] | None = None,
    user_decisions: "UserDecisionsLog | None" = None,
) -> CodingAgentRole:
    tool_allowlist = tools or ["read_file", "execute", "glob_files", "grep"]
    return CodingAgentRole(
        name="verifier",
        system_prompt=_compose(_VERIFIER_PROMPT, tool_allowlist),
        tool_allowlist=tool_allowlist,
        model_tier="fast",
        _user_decisions=user_decisions,
    )


ROLE_FACTORIES = {
    "planner": planner_role,
    "coder": coder_role,
    "reviewer": reviewer_role,
    "fixer": fixer_role,
    "researcher": researcher_role,
    "verifier": verifier_role,
}


__all__ = [
    "CodingAgentRole",
    "ROLE_FACTORIES",
    "coder_role",
    "fixer_role",
    "planner_role",
    "researcher_role",
    "reviewer_role",
    "verifier_role",
]
