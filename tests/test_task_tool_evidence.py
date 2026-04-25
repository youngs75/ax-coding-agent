"""Verifier→fixer evidence auto-prepend (harness 자동 첨부) 단위 테스트.

배경: 이전에는 SYSTEM_PROMPT 가 *"verifier 가 보고한 실패를 fixer description
에 그대로 복사하세요"* 라는 의무를 orchestrator(LLM) 에게 떠넘겼다. v8 의
TASK-03 RBAC 사이클은 LLM 이 이 의무를 잊거나 부정확히 따라 fixer 가 같은
실패를 반복하며 무너졌다.

이 모듈은 그 책임을 ``coding_agent.tools.task_tool.build_task_tool`` 의
wrapper 로 옮긴 뒤 다음을 검증한다:

1. fixer 위임 시 직전 verifier 의 ``_format_verifier_output`` 텍스트가
   description 앞에 자동 prepend 된다.
2. 직전 verifier 결과가 없으면 prepend 안 된다.
3. coder/planner 등 다른 role 위임 시 prepend 안 된다.
4. evidence 가 ``_VERIFIER_EVIDENCE_PREPEND_CAP`` 을 초과하면 ``(truncated)``
   마커로 잘린다.
5. SYSTEM_PROMPT 에 더 이상 "fixer description 에 그대로 복사" 라인이 없다.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import StructuredTool

from minyoung_mah import RoleStatus
from minyoung_mah.core.types import (
    RoleInvocationResult,
    ToolCallRequest,
    ToolResult,
)

import coding_agent.tools.task_tool as task_tool_module
from coding_agent.core.loop import SYSTEM_PROMPT
from coding_agent.subagents.user_decisions import UserDecisionsLog
from coding_agent.tools.task_tool import (
    _VERIFIER_EVIDENCE_PREPEND_CAP,
    _prepend_verifier_evidence,
    build_task_tool,
)


# ---------------------------------------------------------------------------
# Fakes — minyoung_mah 의 build_subagent_task_tool 을 가로채서 inner_tool 의
# ``func`` 가 호출 인자를 capture 하게 만든다.
# ---------------------------------------------------------------------------


class _CapturingInnerTool:
    """Mimics the StructuredTool returned by ``build_subagent_task_tool``.

    Records every call and dispatches the registered ``on_tool_call_end``
    hook so the wrapper's verifier-evidence cache gets populated.
    """

    def __init__(self, hooks: dict[str, Any]):
        self._hooks = hooks
        self.calls: list[dict[str, Any]] = []
        self.next_role: str = "coder"
        self.next_result: RoleInvocationResult | None = None
        self.next_status: str = "COMPLETED"
        self.name = "task"
        self.description = "fake task tool"
        # Match the real schema so the wrapper's StructuredTool.from_function
        # can re-bind it without complaints.
        from minyoung_mah.langgraph.subagent_task_tool import SubAgentTaskInput
        self.args_schema = SubAgentTaskInput

    def func(
        self,
        description: str,
        agent_type: str = "auto",
        tool_call_id: str = "",
    ) -> str:
        self.calls.append(
            {
                "description": description,
                "agent_type": agent_type,
                "tool_call_id": tool_call_id,
            }
        )
        # Fire the on_tool_call_end hook the way the real library does so
        # the wrapper's verifier evidence cache is exercised end-to-end.
        if self.next_result is not None:
            on_end = self._hooks.get("on_tool_call_end")
            if on_end is not None:
                on_end(self.next_role, description, self.next_result, self.next_status)
        return f"[Task {self.next_status} — {self.next_role}]\nfake-output"


def _verifier_failure_result() -> RoleInvocationResult:
    """Build a verifier result with one failing pytest execute."""
    return RoleInvocationResult(
        role_name="verifier",
        status=RoleStatus.COMPLETED,
        output="Scope: ran tests\nResult: 1 failed",
        tool_calls=[
            ToolCallRequest(
                call_id="e0", tool_name="execute", args={"command": "pytest -q tests/test_rbac.py"}
            )
        ],
        tool_results=[
            ToolResult(
                ok=True,
                value=(
                    "FAILED tests/test_rbac.py::test_admin_only_route\n"
                    "AssertionError: expected 403 got 200\n"
                    "[exit code: 1]"
                ),
            )
        ],
    )


@pytest.fixture
def captured_tool(monkeypatch: pytest.MonkeyPatch):
    """Patch ``build_subagent_task_tool`` so we can observe what the wrapper
    forwards to the inner library tool.
    """
    captured: dict[str, Any] = {}

    def _fake_build(orchestrator, **kwargs):  # noqa: ARG001
        captured["hooks"] = kwargs
        inner = _CapturingInnerTool(kwargs)
        captured["inner"] = inner
        return inner

    monkeypatch.setattr(task_tool_module, "build_subagent_task_tool", _fake_build)

    user_decisions = UserDecisionsLog()
    wrapper = build_task_tool(
        orchestrator=None,  # fake_build ignores it
        user_decisions=user_decisions,
        todo_store=None,
    )
    return {"wrapper": wrapper, "inner": captured["inner"], "hooks": captured["hooks"]}


# ---------------------------------------------------------------------------
# Tests — wrapper structural sanity
# ---------------------------------------------------------------------------


def test_wrapper_is_structured_tool(captured_tool):
    assert isinstance(captured_tool["wrapper"], StructuredTool)
    # Same name + schema as the inner tool — orchestrator-facing surface stays
    # identical so prompts/registries don't need updating.
    assert captured_tool["wrapper"].name == "task"
    assert captured_tool["wrapper"].args_schema is captured_tool["inner"].args_schema


# ---------------------------------------------------------------------------
# Tests — _prepend_verifier_evidence helper
# ---------------------------------------------------------------------------


def test_prepend_includes_marker_and_separator():
    out = _prepend_verifier_evidence("TASK-03: fix RBAC", "evidence body")
    assert out.startswith("## 직전 verifier 결과 (harness 자동 첨부)")
    assert "evidence body" in out
    assert "----" in out
    assert out.endswith("TASK-03: fix RBAC")


def test_prepend_truncates_when_evidence_too_long():
    huge = "x" * (_VERIFIER_EVIDENCE_PREPEND_CAP + 5000)
    out = _prepend_verifier_evidence("TASK-03", huge)
    assert "(truncated)" in out
    # Original description still present at the end.
    assert out.endswith("TASK-03")


def test_prepend_does_not_truncate_short_evidence():
    out = _prepend_verifier_evidence("TASK-03", "short")
    assert "(truncated)" not in out


# ---------------------------------------------------------------------------
# Tests — wrapper end-to-end behaviour
# ---------------------------------------------------------------------------


def _invoke(wrapper: StructuredTool, *, description: str, agent_type: str, tool_call_id: str = "tc-1") -> str:
    return wrapper.invoke(
        {
            "name": "task",
            "args": {"description": description, "agent_type": agent_type},
            "id": tool_call_id,
            "type": "tool_call",
        }
    )


def test_no_prepend_when_no_prior_verifier(captured_tool):
    inner = captured_tool["inner"]
    inner.next_role = "fixer"
    _invoke(captured_tool["wrapper"], description="TASK-03: fix bug", agent_type="fixer")

    assert len(inner.calls) == 1
    forwarded = inner.calls[0]["description"]
    assert "harness 자동 첨부" not in forwarded
    assert forwarded == "TASK-03: fix bug"


def test_evidence_prepended_for_fixer_after_verifier(captured_tool):
    inner = captured_tool["inner"]

    # Round 1 — verifier reports a failure.
    inner.next_role = "verifier"
    inner.next_result = _verifier_failure_result()
    inner.next_status = "COMPLETED"
    _invoke(
        captured_tool["wrapper"],
        description="TASK-03: verify RBAC",
        agent_type="verifier",
        tool_call_id="tc-v",
    )

    # Round 2 — fixer delegation. Evidence should be auto-prepended.
    inner.next_role = "fixer"
    inner.next_result = None  # don't re-fire on_end during the fixer call
    inner.next_status = "COMPLETED"
    _invoke(
        captured_tool["wrapper"],
        description="TASK-03: fix the broken admin guard",
        agent_type="fixer",
        tool_call_id="tc-f",
    )

    fixer_call = inner.calls[1]
    forwarded = fixer_call["description"]
    assert "harness 자동 첨부" in forwarded
    assert "FAILED tests/test_rbac.py::test_admin_only_route" in forwarded
    assert "[exit code: 1]" in forwarded
    # Original description still present after the separator.
    assert "TASK-03: fix the broken admin guard" in forwarded


def test_no_prepend_for_coder_even_after_verifier(captured_tool):
    inner = captured_tool["inner"]

    inner.next_role = "verifier"
    inner.next_result = _verifier_failure_result()
    _invoke(
        captured_tool["wrapper"],
        description="TASK-03: verify",
        agent_type="verifier",
        tool_call_id="tc-v",
    )

    # coder description should pass through untouched — only fixer gets the
    # auto-prepend (other roles have their own prompts and tools).
    inner.next_role = "coder"
    inner.next_result = None
    _invoke(
        captured_tool["wrapper"],
        description="TASK-04: implement endpoint",
        agent_type="coder",
        tool_call_id="tc-c",
    )

    coder_call = inner.calls[1]
    assert "harness 자동 첨부" not in coder_call["description"]


def test_no_prepend_for_planner(captured_tool):
    inner = captured_tool["inner"]
    inner.next_role = "verifier"
    inner.next_result = _verifier_failure_result()
    _invoke(captured_tool["wrapper"], description="TASK-01: verify", agent_type="verifier")

    inner.next_role = "planner"
    inner.next_result = None
    _invoke(captured_tool["wrapper"], description="re-plan tasks", agent_type="planner")

    assert "harness 자동 첨부" not in inner.calls[1]["description"]


def test_evidence_refreshed_on_each_verifier_round(captured_tool):
    inner = captured_tool["inner"]

    # First verifier round.
    inner.next_role = "verifier"
    first = _verifier_failure_result()
    first.tool_results[0] = ToolResult(ok=True, value="FAIL_FIRST_ROUND\n[exit code: 1]")
    inner.next_result = first
    _invoke(captured_tool["wrapper"], description="TASK-03 v1", agent_type="verifier")

    # Second verifier round with different evidence.
    second = _verifier_failure_result()
    second.tool_results[0] = ToolResult(ok=True, value="FAIL_SECOND_ROUND\n[exit code: 1]")
    inner.next_result = second
    _invoke(captured_tool["wrapper"], description="TASK-03 v2", agent_type="verifier")

    # Now fixer should see the *second* round's evidence, not the first.
    inner.next_role = "fixer"
    inner.next_result = None
    _invoke(captured_tool["wrapper"], description="TASK-03: fix", agent_type="fixer")

    forwarded = inner.calls[2]["description"]
    assert "FAIL_SECOND_ROUND" in forwarded
    assert "FAIL_FIRST_ROUND" not in forwarded


# ---------------------------------------------------------------------------
# Tests — SYSTEM_PROMPT no longer carries the manual-copy obligation.
# ---------------------------------------------------------------------------


def test_system_prompt_no_longer_demands_manual_evidence_copy():
    # The exact phrase the prompt used to carry. Removing it is the whole
    # point of this refactor — if it sneaks back in, the harness's auto-
    # prepend becomes redundant noise.
    assert "fixer description 에 그대로 복사" not in SYSTEM_PROMPT
    # The 6-회 ProgressGuard reference must still be there (handled by
    # bundle A; we just want to confirm we didn't accidentally delete it).
    assert "6 회 이상" in SYSTEM_PROMPT or "ProgressGuard" in SYSTEM_PROMPT
