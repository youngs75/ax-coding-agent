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

from coding_agent.skills import SKILL_STORE, Skill, render_skill_block

if TYPE_CHECKING:
    from minyoung_mah import InvocationContext

    from coding_agent.subagents.user_decisions import UserDecisionsLog


# ---------------------------------------------------------------------------
# Fork rules — shared output discipline for every SubAgent role.
# ---------------------------------------------------------------------------

_FORK_RULES = """
## Output Rules (MANDATORY)
1. When you finish the task, respond with a brief natural-language summary
   and stop. Do NOT keep calling tools after the task is complete.
2. Do NOT converse, ask questions, or suggest next steps.
3. Stay strictly within the task scope.
4. Do NOT call tools that are not in the available tools list above.

## Language Policy (MANDATORY)
사용자 facing 출력의 기본 언어는 한국어입니다. 사용자가 영어를 명시적으로
요청한 경우에만 영어를 씁니다. 다음은 모두 한국어로 작성하세요:
- 산출 문서와 보고서, 변경 사항 설명
- 사용자에게 보여지는 모든 텍스트 (ask_user_question 의 question/options/description, 에러 메시지, 진행 상태 메시지)
- 코드 안의 주석 (한국어로 의도/이유를 설명; 식별자 이름은 영어 유지)
- 최종 SubAgent 요약문

영어로 작성해도 되는 것:
- 변수/함수/클래스/파일 경로 같은 식별자
- 외부 API 표준 키워드 (HTTP method, JSON key, SQL 키워드 등)
"""


# ---------------------------------------------------------------------------
# Role-specific system prompts (static — {tools} fills in once at role build time).
# ---------------------------------------------------------------------------


_PLANNER_PROMPT = """\
You are a planning agent. Read the task, explore what you need, then produce
the artifact the orchestrator asked you for.

Available tools: {tools}

Follow the procedures in the Skills block of your user message
(planning-workflow covers request reading, ambiguity handling, task
ordering, and artifact shape).
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
- Report any bugs, edge cases, or style issues clearly with concrete file paths and line references.
- Do NOT call tools that are not in the available tools list above.
"""

_FIXER_PROMPT = """\
You are a bug-fixing agent. You fix code — you do NOT run tests, builds, or
any shell command. The verifier runs tests. You only edit or create source
files.

Available tools: {tools}

Follow the procedures in the Skills block of your user message
(fix-discipline covers required inputs, minimal-edit rules, file creation,
and the INCOMPLETE signal).
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

_LEDGER_PROMPT = """\
You are a todo ledger agent. Your only job is to register tasks in the
orchestrator's ledger or update their status. You do not analyze requirements,
decompose tasks, or decide content — you operate exactly on what the
orchestrator gave you.

Available tools: {tools}

Rules:
- Use write_todos when the task description gives you a list of tasks
  to register. Use exactly the ids and contents provided — do not rename,
  reorder, or add tasks.
- Use update_todo when the task description names a specific task id and
  target status.
- If the description is ambiguous (no task list and no clear update target),
  return INCOMPLETE and ask the orchestrator to clarify.
- Do NOT invent new tasks, rewrite content, or call planner-style reasoning.
- Do NOT call tools that are not in the available tools list above.
"""

_VERIFIER_PROMPT = """\
You are a verification agent. Your job is to run whatever checks the task
asks for and report what happened. You do not modify code.

Available tools: {tools}

Follow the procedures in the Skills block of your user message
(verification-report-format covers verbatim evidence, fix-prescription
boundaries, and environment-gap reporting).
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
    # Skill bodies injected into the user message at invocation time. Kept
    # out of ``system_prompt`` so identity stays fixed and procedures stay
    # swappable (see coding_agent/skills/).
    _skills: tuple[Skill, ...] = field(default_factory=tuple)

    def build_user_message(self, invocation: "InvocationContext") -> str:
        parts: list[str] = []
        if self._user_decisions is not None:
            header = self._user_decisions.header()
            if header:
                parts.append(header)

        if self._skills:
            parts.append(render_skill_block(list(self._skills)))

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


def _skills_for(role_name: str) -> tuple[Skill, ...]:
    return tuple(SKILL_STORE.for_role(role_name))


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
        _skills=_skills_for("planner"),
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
        _skills=_skills_for("fixer"),
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
        _skills=_skills_for("verifier"),
    )


def ledger_role(
    tools: list[str] | None = None,
    user_decisions: "UserDecisionsLog | None" = None,
) -> CodingAgentRole:
    tool_allowlist = tools or ["write_todos", "update_todo"]
    return CodingAgentRole(
        name="ledger",
        system_prompt=_compose(_LEDGER_PROMPT, tool_allowlist),
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
    "ledger": ledger_role,
}


__all__ = [
    "CodingAgentRole",
    "ROLE_FACTORIES",
    "coder_role",
    "fixer_role",
    "ledger_role",
    "planner_role",
    "researcher_role",
    "reviewer_role",
    "verifier_role",
]
