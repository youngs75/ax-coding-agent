"""v22.4 — _auto_verify_chain 결과 마커 기반 todo advance.

배경: v22 #2 가 coder COMPLETED 직후 verifier+fixer 사이클을 inner_func
호출로 강제했지만, todo advance 는 *coder 의 _on_end* 에서 즉시 일어났다
→ chain 이 fail 해도 CLI 는 ✓ (v25 회귀, "CLI ✓ 의 거짓말").

v22.4 처방:
- _on_end: coder COMPLETED 만으로 advance 보류
- _run_wrapped: _auto_verify_chain 종료 후 결과 본문의 마커로 advance
  - ``_AUTO_VERIFY_PASSED_MARKER`` → ``completed``
  - ``_AUTO_VERIFY_FAILED_MARKER`` → ``verify_failed`` (신규 status)

이 모듈은 그 advance 결정의 단위 검증.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import StructuredTool

import coding_agent.tools.task_tool as task_tool_module
from coding_agent.subagents.user_decisions import UserDecisionsLog
from coding_agent.tools.task_tool import (
    _AUTO_VERIFY_FAILED_MARKER,
    _AUTO_VERIFY_PASSED_MARKER,
    build_task_tool,
)
from coding_agent.tools.todo_tool import TodoItem, TodoStore


class _SilentCoderInnerTool:
    """``inner_func`` 이 coder 로 분기하면 COMPLETED 본문 반환. 이후
    ``_run_wrapped`` 가 _auto_verify_chain 으로 진입한다 (그건 monkeypatch
    로 별도 stub).
    """

    def __init__(self, hooks: dict[str, Any]) -> None:
        self._hooks = hooks
        self.calls: list[dict[str, Any]] = []
        self.name = "task"
        self.description = "fake task tool (coder-only)"
        from minyoung_mah.langgraph.subagent_task_tool import SubAgentTaskInput
        self.args_schema = SubAgentTaskInput

    def func(
        self,
        description: str,
        agent_type: str = "auto",
        tool_call_id: str = "",
    ) -> str:
        self.calls.append({"description": description, "agent_type": agent_type})
        on_start = self._hooks.get("on_tool_call_start")
        if on_start is not None:
            on_start("coder", description)
        # _on_end 발화 — coder COMPLETED 라도 v22.4 에서는 advance 안 됨
        return "[Task COMPLETED — coder]\nfake-coder-output"


@pytest.fixture
def coder_wrapper(monkeypatch: pytest.MonkeyPatch):
    """todo_store 포함 wrapper — _auto_verify_chain 은 테스트가 stub."""
    captured: dict[str, Any] = {}

    def _fake_build(orchestrator, **kwargs):  # noqa: ARG001
        captured["hooks"] = kwargs
        inner = _SilentCoderInnerTool(kwargs)
        captured["inner"] = inner
        return inner

    monkeypatch.setattr(task_tool_module, "build_subagent_task_tool", _fake_build)

    store = TodoStore()
    store.replace([
        TodoItem(id="TASK-07", content="implement endpoint"),
    ])

    user_decisions = UserDecisionsLog()
    wrapper = build_task_tool(
        orchestrator=None,
        user_decisions=user_decisions,
        todo_store=store,
    )
    return {
        "wrapper": wrapper,
        "inner": captured["inner"],
        "store": store,
    }


def _invoke_coder(wrapper: StructuredTool, *, description: str) -> Any:
    return wrapper.invoke(
        {
            "name": "task",
            "args": {"description": description, "agent_type": "coder"},
            "id": "tc-coder",
            "type": "tool_call",
        }
    )


def test_coder_completed_alone_does_not_advance(
    coder_wrapper, monkeypatch: pytest.MonkeyPatch
):
    """coder COMPLETED → _on_end 는 advance 보류. chain 마커가 없으면
    in_progress (start hook 가 마킹) 그대로 — completed 가 아니라는 게 핵심."""
    # _auto_verify_chain 이 coder result 를 그대로 반환 — 마커 없음
    monkeypatch.setattr(
        task_tool_module,
        "_auto_verify_chain",
        lambda **kw: kw["coder_result"],  # passthrough
    )
    store: TodoStore = coder_wrapper["store"]
    _invoke_coder(coder_wrapper["wrapper"], description="TASK-07: 구현")

    # _on_start 의 advance 로 in_progress, _on_end 의 coder COMPLETED 분기에선
    # 보류 → completed 로 가지 않는 게 v22.4 의 핵심.
    assert store.list_items()[0].status == "in_progress"


def test_chain_pass_marker_advances_to_completed(
    coder_wrapper, monkeypatch: pytest.MonkeyPatch
):
    """auto-chain 결과에 _AUTO_VERIFY_PASSED_MARKER 가 있으면 completed."""

    def _fake_chain(**kw):
        return (
            kw["coder_result"]
            + f"\n## ↳ harness auto-verifier {_AUTO_VERIFY_PASSED_MARKER} (1/3)\n"
        )

    monkeypatch.setattr(task_tool_module, "_auto_verify_chain", _fake_chain)
    store: TodoStore = coder_wrapper["store"]
    _invoke_coder(coder_wrapper["wrapper"], description="TASK-07: 구현")

    assert store.list_items()[0].status == "completed"


def test_chain_failed_marker_advances_to_verify_failed(
    coder_wrapper, monkeypatch: pytest.MonkeyPatch
):
    """auto-chain 결과에 _AUTO_VERIFY_FAILED_MARKER 가 있으면 verify_failed.

    v22.4 의 핵심 — v25 회귀 ("CLI ✓ 의 거짓말") 차단."""

    def _fake_chain(**kw):
        return (
            kw["coder_result"]
            + f"\n## ↳ harness auto-verifier {_AUTO_VERIFY_FAILED_MARKER} (3회 실패)\n"
        )

    monkeypatch.setattr(task_tool_module, "_auto_verify_chain", _fake_chain)
    store: TodoStore = coder_wrapper["store"]
    _invoke_coder(coder_wrapper["wrapper"], description="TASK-07: 구현")

    assert store.list_items()[0].status == "verify_failed"
    assert store.counts()["verify_failed"] == 1


def test_chain_no_marker_keeps_in_progress(
    coder_wrapper, monkeypatch: pytest.MonkeyPatch
):
    """chain 결과에 마커가 모두 없으면 completed 로 가지 않음 (안전한 기본값).

    in_progress 는 _on_start 가 마킹 — 그 자체는 v22.4 의 변경 대상 아님.
    """
    monkeypatch.setattr(
        task_tool_module,
        "_auto_verify_chain",
        lambda **kw: kw["coder_result"] + "\n(no marker)\n",
    )
    store: TodoStore = coder_wrapper["store"]
    _invoke_coder(coder_wrapper["wrapper"], description="TASK-07: 구현")

    final = store.list_items()[0].status
    assert final != "completed"
    assert final != "verify_failed"


def test_chain_pass_with_no_task_id_in_description(
    coder_wrapper, monkeypatch: pytest.MonkeyPatch
):
    """description 에 TASK-NN 이 없으면 advance 시도 자체 skip — KeyError 안 남."""
    monkeypatch.setattr(
        task_tool_module,
        "_auto_verify_chain",
        lambda **kw: kw["coder_result"]
        + f"\n{_AUTO_VERIFY_PASSED_MARKER}\n",
    )
    store: TodoStore = coder_wrapper["store"]
    _invoke_coder(coder_wrapper["wrapper"], description="some untracked work")

    # 기존 TASK-07 status 변동 없음
    assert store.list_items()[0].status == "pending"
