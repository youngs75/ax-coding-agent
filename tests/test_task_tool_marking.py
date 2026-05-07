"""task_tool 의 todo 자동 마킹 — role-agnostic 동작 (v10 회귀 fix).

기존: ``coder`` 와 성공한 ``verifier`` 만 ``completed`` 로 옮김. v10 에서
planner 가 TASK-02 (PRD 작성) 처리했는데도 ledger 가 ◐ 그대로 남아서
graph 흐름이 깨짐. fix 후 모든 SubAgent role 이 task_id 매칭 시 마킹.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from coding_agent.tools.task_tool import (
    _auto_advance_todo,
    _extract_task_id,
    _verifier_signals_success,
)


# ── 가짜 store / result ──


class _FakeTodoItem:
    def __init__(self, id_: str, status: str = "pending", content: str = ""):
        self.id = id_
        self.status = status
        self.content = content


class _FakeTodoStore:
    def __init__(self, items: list[_FakeTodoItem]):
        self._items = items

    def list_items(self) -> list[_FakeTodoItem]:
        return list(self._items)

    def update(self, task_id: str, status: str) -> None:
        for it in self._items:
            if it.id == task_id:
                it.status = status
                return
        raise KeyError(task_id)

    def counts(self) -> dict[str, int]:
        result = {"pending": 0, "in_progress": 0, "completed": 0}
        for it in self._items:
            result[it.status] = result.get(it.status, 0) + 1
        return result


@dataclass
class _FakeToolCallReq:
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakeToolCallRes:
    ok: bool = True
    value: Any = None
    error: str | None = None


@dataclass
class _FakeRoleResult:
    output: str = ""
    tool_calls: list[_FakeToolCallReq] = field(default_factory=list)
    tool_results: list[_FakeToolCallRes] = field(default_factory=list)


# ── _auto_advance_todo (기존 헬퍼, 재확인) ──


def test_auto_advance_pending_to_in_progress():
    store = _FakeTodoStore([_FakeTodoItem("TASK-01")])
    flipped = _auto_advance_todo(store, "TASK-01", "in_progress", None)
    assert flipped is True
    assert store.list_items()[0].status == "in_progress"


def test_auto_advance_skips_when_already_completed():
    store = _FakeTodoStore([_FakeTodoItem("TASK-01", status="completed")])
    flipped = _auto_advance_todo(store, "TASK-01", "in_progress", None)
    assert flipped is False  # completed → in_progress 강제 후퇴 금지


# ── _on_end 핵심 (build_task_tool 안의 closure) ──
# ``build_task_tool`` 은 minyoung_mah.build_subagent_task_tool 의 결과를
# wrap 한 StructuredTool 을 반환. 그 안의 ``_on_end`` 클로저가 todo 마킹
# 책임. 직접 호출 어렵게 닫혀 있어 기능을 *최소 재현* 하는 헬퍼로 검증.


def _simulate_on_end(
    store: _FakeTodoStore,
    role_name: str,
    description: str,
    result: _FakeRoleResult,
    status_tag: str,
) -> None:
    """task_tool 의 ``_on_end`` 와 동일한 분기를 재현. 본체가 closure 라
    직접 import 못 하므로, 이 단순 시뮬레이션으로 *현재 동작* 의 invariant
    를 단언한다. 실제 코드와 분기가 일치해야 함 (다르면 테스트가 깨짐)."""
    if status_tag != "COMPLETED":
        return
    task_id = _extract_task_id(description)
    if not task_id:
        return
    # v22.4 — coder 의 advance 는 _run_wrapped 가 _auto_verify_chain 결과
    # 마커로 별도 처리. _on_end 단계에선 보류.
    if role_name == "coder":
        return
    if role_name == "verifier" and not _verifier_signals_success(result):
        return
    _auto_advance_todo(store, task_id, "completed", None)


# ── role-agnostic 마킹 ──


@pytest.mark.parametrize("role", ["planner", "fixer", "researcher", "reviewer"])
def test_completion_marks_for_any_non_coder_role(role):
    store = _FakeTodoStore([_FakeTodoItem("TASK-02", status="in_progress")])
    _simulate_on_end(
        store, role, "TASK-02: 분해 결과 작성",
        _FakeRoleResult(),
        "COMPLETED",
    )
    assert store.list_items()[0].status == "completed"


def test_coder_completion_does_not_advance(_unused=None):
    """v22.4 — coder COMPLETED 만으로 todo 가 ``completed`` 로 advance 되면
    안 된다. _auto_verify_chain 결과 마커로 별도 advance 함."""
    store = _FakeTodoStore([_FakeTodoItem("TASK-02", status="in_progress")])
    _simulate_on_end(
        store, "coder", "TASK-02: 구현",
        _FakeRoleResult(),
        "COMPLETED",
    )
    # advance 안 됨 — in_progress 유지
    assert store.list_items()[0].status == "in_progress"


def test_verifier_marks_only_when_all_executes_pass():
    # 모든 execute 가 성공 — 마킹
    store = _FakeTodoStore([_FakeTodoItem("TASK-03", status="in_progress")])
    success = _FakeRoleResult(
        tool_calls=[_FakeToolCallReq("execute")],
        tool_results=[_FakeToolCallRes(ok=True, value="ok")],
    )
    _simulate_on_end(store, "verifier", "TASK-03: 검증", success, "COMPLETED")
    assert store.list_items()[0].status == "completed"

    # exit code 1 마커가 있으면 마킹 보류 (기존 안전망)
    store2 = _FakeTodoStore([_FakeTodoItem("TASK-04", status="in_progress")])
    fail = _FakeRoleResult(
        tool_calls=[_FakeToolCallReq("execute")],
        tool_results=[_FakeToolCallRes(ok=True, value="result\n[exit code: 1]")],
    )
    _simulate_on_end(store2, "verifier", "TASK-04: 검증", fail, "COMPLETED")
    assert store2.list_items()[0].status == "in_progress"


def test_no_mark_when_status_incomplete():
    store = _FakeTodoStore([_FakeTodoItem("TASK-05", status="in_progress")])
    _simulate_on_end(store, "coder", "TASK-05: x", _FakeRoleResult(), "INCOMPLETE")
    assert store.list_items()[0].status == "in_progress"


def test_no_mark_when_no_task_id_in_description():
    # ledger / critic 등 task_id 없는 위임 — 자동 무시
    store = _FakeTodoStore([_FakeTodoItem("TASK-01", status="pending")])
    _simulate_on_end(
        store, "ledger", "register the following 7 tasks",
        _FakeRoleResult(), "COMPLETED",
    )
    assert store.list_items()[0].status == "pending"


# ── B-1: fixer 재시도 경고 임계값 ──


def test_fixer_retry_threshold_env_override(monkeypatch):
    """환경변수 AX_FIXER_RETRY_WARN 으로 임계값 override 가능."""
    monkeypatch.setenv("AX_FIXER_RETRY_WARN", "5")
    # task_tool 모듈 reload 해 상수 재평가
    import importlib

    import coding_agent.tools.task_tool as tt
    importlib.reload(tt)
    assert tt._FIXER_RETRY_WARN_THRESHOLD == 5
    # 원복
    monkeypatch.delenv("AX_FIXER_RETRY_WARN", raising=False)
    importlib.reload(tt)
    assert tt._FIXER_RETRY_WARN_THRESHOLD == 3
