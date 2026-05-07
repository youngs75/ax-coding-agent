"""Sufficiency critic — invoke_role 응답 파싱 + 폴백 시나리오.

orchestrator 는 mock 으로 대체. invoke_role 결과 텍스트의 4가지 정상
verdict 와 잘못된 JSON / 필드 누락 / 알 수 없는 verdict 폴백을 모두
``escalate_hitl`` 로 정규화하는지 확인한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from coding_agent.sufficiency.critic import _parse_verdict, invoke_critic


# ── _parse_verdict (pure) ──────────────────────────────────────────────────


def test_parse_verdict_pass():
    raw = '{"verdict":"pass","target_role":null,"reason":"OK","feedback_for_retry":null}'
    v = _parse_verdict(raw)
    assert v.verdict == "pass"
    assert v.target_role is None
    assert v.feedback_for_retry is None


def test_parse_verdict_retry_with_target_role():
    raw = (
        '{"verdict":"retry_lookup","target_role":"coder",'
        '"reason":"todo 누락","feedback_for_retry":"남은 task 처리"}'
    )
    v = _parse_verdict(raw)
    assert v.verdict == "retry_lookup"
    assert v.target_role == "coder"
    assert v.feedback_for_retry == "남은 task 처리"


def test_parse_verdict_replan_planner():
    raw = (
        '{"verdict":"replan","target_role":"planner",'
        '"reason":"분해 누락","feedback_for_retry":"재분해"}'
    )
    v = _parse_verdict(raw)
    assert v.verdict == "replan"
    assert v.target_role == "planner"


def test_parse_verdict_escalate():
    raw = (
        '{"verdict":"escalate_hitl","target_role":null,'
        '"reason":"모호","feedback_for_retry":null}'
    )
    v = _parse_verdict(raw)
    assert v.verdict == "escalate_hitl"
    assert v.target_role is None


def test_parse_verdict_with_json_fence():
    raw = '머리말\n```json\n{"verdict":"pass","target_role":null,"reason":"OK"}\n```\n꼬리'
    v = _parse_verdict(raw)
    assert v.verdict == "pass"


def test_parse_verdict_with_leading_prose():
    raw = (
        '제 평가는 다음과 같습니다:\n'
        '{"verdict":"retry_lookup","target_role":"fixer","reason":"x","feedback_for_retry":"y"}'
    )
    v = _parse_verdict(raw)
    assert v.verdict == "retry_lookup"
    assert v.target_role == "fixer"


# ── 폴백: 잘못된 JSON / 필드 / 값 ──────────────────────────────────────────


def test_parse_verdict_garbage_falls_back_to_escalate():
    v = _parse_verdict("complete garbage no JSON here")
    assert v.verdict == "escalate_hitl"
    assert v.target_role is None
    assert "찾지 못" in v.reason or "JSON" in v.reason


def test_parse_verdict_empty_falls_back_to_escalate():
    v = _parse_verdict("")
    assert v.verdict == "escalate_hitl"


def test_parse_verdict_unknown_verdict_falls_back():
    raw = '{"verdict":"maybe","target_role":null,"reason":"x"}'
    v = _parse_verdict(raw)
    assert v.verdict == "escalate_hitl"
    assert "maybe" in v.reason or "verdict" in v.reason.lower()


def test_parse_verdict_invalid_target_role_normalized_to_none():
    raw = '{"verdict":"retry_lookup","target_role":"some_alien","reason":"x"}'
    v = _parse_verdict(raw)
    assert v.verdict == "retry_lookup"  # verdict 자체는 살림
    assert v.target_role is None  # 잘못된 target 은 None


def test_parse_verdict_missing_reason_uses_default():
    raw = '{"verdict":"pass","target_role":null}'
    v = _parse_verdict(raw)
    assert v.verdict == "pass"
    assert v.reason  # 기본 메시지가 채워짐


def test_parse_verdict_string_null_for_target_role():
    """LLM 이 'null' 문자열로 반환해도 None 으로 정규화."""
    raw = '{"verdict":"pass","target_role":"null","reason":"x","feedback_for_retry":"null"}'
    v = _parse_verdict(raw)
    assert v.target_role is None
    assert v.feedback_for_retry is None


# ── invoke_critic — orchestrator mock ───────────────────────────────────────


@dataclass
class _FakeRoleResult:
    output: str | None


class _FakeOrchestrator:
    """invoke_role 만 흉내. critic.py 가 다른 attribute 안 보는 걸 확인."""

    def __init__(self, output_text: str | None = None, raise_exc: Exception | None = None):
        self._output = output_text
        self._raise = raise_exc
        self.call_count = 0
        self.last_invocation: Any = None

    async def invoke_role(self, role_name, invocation):  # noqa: ANN001
        assert role_name == "critic"
        self.call_count += 1
        self.last_invocation = invocation
        if self._raise:
            raise self._raise
        return _FakeRoleResult(output=self._output)


@pytest.mark.asyncio
async def test_invoke_critic_returns_pass():
    orch = _FakeOrchestrator(
        output_text='{"verdict":"pass","target_role":null,"reason":"OK"}'
    )
    v = await invoke_critic(
        orch,  # type: ignore[arg-type]
        user_request="유저 요청 텍스트",
        metrics={"pytest_exit": 0},
        iteration=1,
    )
    assert v.verdict == "pass"
    assert orch.call_count == 1
    assert orch.last_invocation.metadata["sufficiency"] is True
    assert "iteration 1" in orch.last_invocation.task_summary
    assert "유저 요청 텍스트" in orch.last_invocation.task_summary
    assert "pytest_exit" in orch.last_invocation.task_summary


@pytest.mark.asyncio
async def test_invoke_critic_handles_orchestrator_exception():
    orch = _FakeOrchestrator(raise_exc=RuntimeError("invoke 실패"))
    v = await invoke_critic(
        orch,  # type: ignore[arg-type]
        user_request="x",
        metrics={},
        iteration=2,
    )
    assert v.verdict == "escalate_hitl"
    assert "invoke 실패" in v.reason


@pytest.mark.asyncio
async def test_invoke_critic_handles_none_output():
    orch = _FakeOrchestrator(output_text=None)
    v = await invoke_critic(
        orch,  # type: ignore[arg-type]
        user_request="x",
        metrics={},
        iteration=1,
    )
    assert v.verdict == "escalate_hitl"


@pytest.mark.asyncio
async def test_invoke_critic_passes_metrics_into_summary():
    orch = _FakeOrchestrator(
        output_text='{"verdict":"pass","target_role":null,"reason":"OK"}'
    )
    metrics = {
        "pytest_exit": 1,
        "lint_errors": 5,
        "todo_ratio": 0.6,
    }
    await invoke_critic(
        orch,  # type: ignore[arg-type]
        user_request="x",
        metrics=metrics,
        iteration=1,
    )
    summary = orch.last_invocation.task_summary
    for k in metrics:
        assert k in summary
