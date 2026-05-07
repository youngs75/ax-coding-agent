"""Sufficiency loop end-to-end — signals → rules → critic → loop helpers.

LangGraph 노드 closure 에 직접 접근 어려우므로 graph 빌드 가능 여부 +
4 시나리오를 모듈 단위로 묶어 검증한다 (실제 LLM 호출 없음):

- HIGH    pass : signals → HIGH gate → 종료 흐름
- MEDIUM  pass : signals → MEDIUM gate → critic mock pass → 종료
- MEDIUM  esc  : signals → MEDIUM gate → critic mock escalate → notify + 종료
- LOW     cyc  : signals → LOW heuristic → 같은 verdict 반복 → cycle escalate

graph 통합은 ``test_graph_builds_with_sufficiency_*`` 두 케이스로 sanity.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from coding_agent.sufficiency.critic import invoke_critic
from coding_agent.sufficiency.loop import (
    compute_cycle_hash,
    detect_cycle,
    emit_critic_verdict_event,
    force_escalate_if_blocked,
    notify_hitl_escalation,
)
from coding_agent.sufficiency.rules import evaluate, heuristic_verdict_for_low
from coding_agent.sufficiency.signals import collect_signals


_DEFAULTS = dict(high_todo=0.9, low_todo=0.5)


# ── 가짜 인프라 ─────────────────────────────────────────────────────────────


@dataclass
class _FakeRoleResult:
    output: str | None


class _FakeOrchestrator:
    def __init__(self, output_text: str | None) -> None:
        self.output_text = output_text
        self.observer = _RecorderObserver()
        self.hitl = _RecorderHITL()

    async def invoke_role(self, role_name, invocation):  # noqa: ANN001
        assert role_name == "critic"
        return _FakeRoleResult(output=self.output_text)


class _RecorderObserver:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event):  # noqa: ANN001
        self.events.append(event)


class _RecorderHITL:
    def __init__(self) -> None:
        self.notifications: list[Any] = []

    async def notify(self, event):  # noqa: ANN001
        self.notifications.append(event)


class _FakeTodoStore:
    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts

    def counts(self) -> dict[str, int]:
        return dict(self._counts)


def _verifier_msg(exit_codes: list[int], with_pairs: bool = True) -> ToolMessage:
    """task_tool 의 verifier ToolMessage 본문 형태를 흉내."""
    head = "[Task COMPLETED — verifier]\n"
    body = "scope: 테스트 실행\nresult: 보고\n\n### execute(command, result) pairs\n"
    if not with_pairs:
        body = ""
    for ec in exit_codes:
        body += f"- command: pytest\n  result: ...\n  output [exit code: {ec}]\n"
    return ToolMessage(content=head + body, tool_call_id="t1", name="task")


# ── signals.collect_signals (messages 파싱) ────────────────────────────────


def test_collect_signals_pytest_pass(tmp_path):
    state = {
        "messages": [
            HumanMessage(content="user request"),
            _verifier_msg([0]),
        ],
        "working_directory": str(tmp_path),
    }
    todo = _FakeTodoStore({"completed": 9, "pending": 1})
    s = collect_signals(state, todo)
    assert s["pytest_exit"] == 0
    assert s["todo_done"] == 9
    assert s["todo_total"] == 10
    assert abs(s["todo_ratio"] - 0.9) < 1e-6
    # prd_coverage 키는 폐기 (R-003) — 결정론으로 처리할 수 없는 영역.
    assert "prd_coverage" not in s


def test_collect_signals_pytest_fail(tmp_path):
    state = {
        "messages": [
            HumanMessage(content="x"),
            _verifier_msg([0, 1]),  # 한 케이스 실패
        ],
        "working_directory": str(tmp_path),
    }
    todo = _FakeTodoStore({"completed": 5, "pending": 0})
    s = collect_signals(state, todo)
    # 가장 큰 절댓값 exit code 가 들어와야 함 (1 > 0)
    assert s["pytest_exit"] == 1


def test_collect_signals_no_verifier_message(tmp_path):
    state = {
        "messages": [HumanMessage(content="x")],
        "working_directory": str(tmp_path),
    }
    todo = _FakeTodoStore({})
    s = collect_signals(state, todo)
    assert s["pytest_exit"] is None
    assert s["lint_errors"] is None
    assert s["todo_total"] == 0
    assert s["todo_ratio"] == 1.0  # ledger 미사용 → 1.0


def test_collect_signals_detects_artifact_intent_v12_pattern(tmp_path):
    """v12 회귀: 사용자가 PRD 작성 + 분해를 명시적 요청했는데 SubAgent 가
    ask 만 하고 종료. 산출물 검증으로 LOW 자동 분류 → planner replan 트리거."""
    user_request = (
        "PMS 시스템을 만들어줘. PRD 파일을 만들고 작업을 원자 단위로 "
        "분해할 것. Spec Driven Development 기반."
    )
    state = {
        "messages": [HumanMessage(content=user_request)],
        "working_directory": str(tmp_path),  # 비어 있는 워크스페이스
    }
    todo = _FakeTodoStore({})  # ledger 도 비어있음
    sigs = collect_signals(state, todo)

    # 사용자가 명시한 모든 산출물이 누락된 상태
    assert "prd" in sigs["artifact_intent"]
    assert "spec" in sigs["artifact_intent"]
    assert "ledger" in sigs["artifact_intent"]
    assert "prd" in sigs["artifacts_missing"]
    assert "spec" in sigs["artifacts_missing"]
    assert "ledger" in sigs["artifacts_missing"]


def test_collect_signals_artifact_present_when_file_exists(tmp_path):
    """PRD.md 가 워크스페이스에 있으면 prd 가 missing 에서 제외됨."""
    (tmp_path / "PRD.md").write_text("# PRD\n", encoding="utf-8")
    state = {
        "messages": [HumanMessage(content="PRD 작성하고 spec 도 만들어")],
        "working_directory": str(tmp_path),
    }
    sigs = collect_signals(state, _FakeTodoStore({}))
    assert "prd" not in sigs["artifacts_missing"]
    assert "spec" in sigs["artifacts_missing"]


def test_collect_signals_ledger_present_when_todos_registered(tmp_path):
    """ledger 가 채워졌으면 (사용자가 분해 요청 + ledger 등록 완료) ledger
    artifact 가 present 로 처리."""
    state = {
        "messages": [HumanMessage(content="원자 단위로 분해해서 ledger 등록")],
        "working_directory": str(tmp_path),
    }
    todo = _FakeTodoStore({"pending": 5})  # ledger 비어있지 않음
    sigs = collect_signals(state, todo)
    assert "ledger" in sigs["artifact_intent"]
    assert "ledger" not in sigs["artifacts_missing"]


def test_collect_signals_no_artifact_intent_when_simple_request(tmp_path):
    """짧은 코드 수정 요청은 산출물 의도 없음 → false positive 방지."""
    state = {
        "messages": [HumanMessage(content="hello world 출력하는 함수 작성")],
        "working_directory": str(tmp_path),
    }
    sigs = collect_signals(state, _FakeTodoStore({}))
    assert sigs["artifact_intent"] == []
    assert sigs["artifacts_missing"] == []


# ── 시나리오 1: HIGH pass ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_high_pass(tmp_path):
    state = {
        "messages": [
            HumanMessage(content="quick fix"),
            _verifier_msg([0]),
        ],
        "working_directory": str(tmp_path),
    }
    todo = _FakeTodoStore({"completed": 9, "pending": 1})
    sigs = collect_signals(state, todo)
    gate = evaluate(sigs, **_DEFAULTS)
    assert gate.level == "HIGH"
    # HIGH 분기는 critic 호출 없이 종료 — apply 노드는 sufficiency_pass
    # 로 처리되지만 라우팅 함수가 직접 extract_memory_final 로 보낸다.


# ── 시나리오 2: MEDIUM critic pass ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_medium_critic_pass(tmp_path):
    state = {
        "messages": [
            HumanMessage(content="ambiguous request"),
            _verifier_msg([0]),
        ],
        "working_directory": str(tmp_path),
    }
    todo = _FakeTodoStore({"completed": 7, "pending": 3})  # ratio 0.7 → MEDIUM
    sigs = collect_signals(state, todo)
    gate = evaluate(sigs, **_DEFAULTS)
    assert gate.level == "MEDIUM"

    orch = _FakeOrchestrator(
        output_text='{"verdict":"pass","target_role":null,"reason":"OK"}'
    )
    verdict = await invoke_critic(
        orch, user_request="x", metrics=gate.metrics, iteration=1,
    )
    assert verdict.verdict == "pass"

    # observer 이벤트가 정상 발화
    await emit_critic_verdict_event(
        orch.observer, verdict=verdict, iteration=1,
        rule_level=gate.level, metrics=gate.metrics,
    )
    assert len(orch.observer.events) == 1
    assert orch.observer.events[0].metadata["verdict"] == "pass"


# ── 시나리오 3: MEDIUM critic escalate → notify ────────────────────────────


@pytest.mark.asyncio
async def test_scenario_medium_critic_escalate(tmp_path):
    state = {
        "messages": [
            HumanMessage(content="ambiguous"),
            _verifier_msg([0]),
        ],
        "working_directory": str(tmp_path),
    }
    todo = _FakeTodoStore({"completed": 7, "pending": 3})
    sigs = collect_signals(state, todo)
    gate = evaluate(sigs, **_DEFAULTS)
    assert gate.level == "MEDIUM"

    orch = _FakeOrchestrator(
        output_text=(
            '{"verdict":"escalate_hitl","target_role":null,'
            '"reason":"모호한 요구사항","feedback_for_retry":null}'
        )
    )
    verdict = await invoke_critic(
        orch, user_request="x", metrics=gate.metrics, iteration=1,
    )
    assert verdict.verdict == "escalate_hitl"

    await notify_hitl_escalation(
        orch.hitl, verdict=verdict, iteration=1, metrics=gate.metrics,
    )
    assert len(orch.hitl.notifications) == 1
    assert orch.hitl.notifications[0].kind == "critic_escalate"
    assert "모호한 요구사항" in orch.hitl.notifications[0].data["reason"]


# ── 시나리오 4: LOW heuristic → 사이클 → escalate 강제 ────────────────────


def test_scenario_low_cycle_escalation(tmp_path):
    """반복적 LOW 결정 (예: pytest 가 계속 실패) — cycle 감지로 escalate."""
    state = {
        "messages": [
            HumanMessage(content="x"),
            _verifier_msg([1]),  # pytest fail
        ],
        "working_directory": str(tmp_path),
    }
    todo = _FakeTodoStore({"completed": 5, "pending": 5})  # 0.5 일단
    sigs = collect_signals(state, todo)
    gate = evaluate(sigs, **_DEFAULTS)
    assert gate.level == "LOW"

    # iteration 1 — 휴리스틱이 fixer retry 결정
    v1 = heuristic_verdict_for_low(gate)
    assert v1.verdict == "retry_lookup"
    assert v1.target_role == "fixer"

    # 첫 entry 등록
    history: list[dict[str, Any]] = []
    h1 = compute_cycle_hash(gate.level, v1.verdict, v1.target_role)
    history.append({"cycle_hash": h1})

    # iteration 2 — 같은 LOW + 같은 (verdict, target) → cycle
    v2 = heuristic_verdict_for_low(gate)
    h2 = compute_cycle_hash(gate.level, v2.verdict, v2.target_role)
    is_cycle = detect_cycle(history, h2)
    assert is_cycle is True

    promoted = force_escalate_if_blocked(
        v2, iteration=2, max_iterations=3, is_cycle=True,
    )
    assert promoted.verdict == "escalate_hitl"
    assert "사이클" in promoted.reason


# ── graph 빌드 — sufficiency_enabled=off / on ──────────────────────────────


def _rebuild_loop_with(env_value: str):
    os.environ["AX_SUFFICIENCY_ENABLED"] = env_value
    from coding_agent import config as _c
    _c._config = None
    from coding_agent.core.loop import AgentLoop
    return AgentLoop()


def test_graph_builds_with_sufficiency_off():
    loop = _rebuild_loop_with("0")
    graph = loop._graph
    nodes = set(graph.get_graph().nodes.keys())
    # sufficiency 노드 자체는 등록되지만 도달 불가 — graph 빌드는 성공
    assert {"sufficiency_gate", "critic", "sufficiency_apply"}.issubset(nodes)


def test_graph_builds_with_sufficiency_on():
    loop = _rebuild_loop_with("1")
    graph = loop._graph
    nodes = set(graph.get_graph().nodes.keys())
    assert {"sufficiency_gate", "critic", "sufficiency_apply"}.issubset(nodes)
    # critic role 이 등록됐는지 — RoleRegistry 내부 _roles dict 확인
    role_dict = getattr(loop._orchestrator.roles, "_roles", {})
    assert "critic" in role_dict
