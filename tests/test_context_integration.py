"""ContextManager + agent_node 통합 — 옛 _trim_orchestrator_messages 대체 검증.

minyoung-mah 0.1.9 의 ContextManager 가 ax agent_node 직전에 호출되어
- 임계값 미달이면 원본 보존 (compacted=False)
- 도달하면 boundary marker + summary 로 교체 (compacted=True)
- circuit breaker 가 consecutive failures 후 skip 으로 전환
하는지를 *실제 AgentLoop* 인스턴스화 + LLM mock 으로 종단 검증한다.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)

from minyoung_mah.context import (
    CompactPolicy,
    ContextManager,
    default_policy,
    get_context_window,
)
from minyoung_mah.context.boundary import is_boundary_message, is_compact_summary


class _FakeModel:
    model_name = "claude-opus-4-7"

    def __init__(self, token_per_msg: int = 1000) -> None:
        self.token_per_msg = token_per_msg
        self.invocations: list[list[BaseMessage]] = []

    def get_num_tokens_from_messages(self, messages: list[BaseMessage]) -> int:
        return len(messages) * self.token_per_msg

    async def ainvoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.invocations.append(list(messages))
        return AIMessage(
            content=(
                "<analysis>analysis</analysis>\n"
                "<summary>compact summary body</summary>"
            )
        )


def _build_messages(n: int) -> list[BaseMessage]:
    msgs: list[BaseMessage] = [
        SystemMessage(content="agent system"),
        HumanMessage(content="initial request"),
    ]
    for i in range(n):
        msgs.append(AIMessage(content=f"ai turn {i}"))
        msgs.append(HumanMessage(content=f"user turn {i}"))
    return msgs


# ── 옛 _trim_orchestrator_messages 제거 확인 ──────────────────────────────────


def test_old_trim_function_removed():
    """옛 휴리스틱 (_trim_orchestrator_messages, _ORCH_MAX_MESSAGES) 가
    완전히 제거되어야 한다."""
    from coding_agent.core import loop as loop_module

    assert not hasattr(loop_module, "_trim_orchestrator_messages")
    assert not hasattr(loop_module, "_ORCH_MAX_MESSAGES")


# ── ContextManager 가 AgentLoop 에 인스턴스화되는지 ──────────────────────────


def test_agent_loop_initializes_context_manager():
    from coding_agent.core.loop import AgentLoop

    loop = AgentLoop()
    assert loop._context_manager is not None
    assert isinstance(loop._context_manager, ContextManager)
    # observer 와 wired
    assert loop._context_manager.observer is loop._orchestrator.observer


# ── compact_if_needed: below threshold → skip ───────────────────────────────


@pytest.mark.asyncio
async def test_compact_skips_when_below_threshold():
    """임계값 미달 — 원본 그대로 + LLM 호출 0."""
    fake = _FakeModel(token_per_msg=100)
    cm = ContextManager(compact_model=fake)
    msgs = _build_messages(2)  # 6 messages * 100 = 600 tokens

    result = await cm.compact_if_needed(msgs, fake)
    assert result.compacted is False
    assert result.reason == "below_threshold"
    assert result.messages == msgs
    assert len(fake.invocations) == 0


# ── compact_if_needed: above threshold → boundary + summary ─────────────────


@pytest.mark.asyncio
async def test_compact_replaces_with_boundary_and_summary():
    """임계값 도달 시 head + boundary + summary + tail 로 교체."""
    fake = _FakeModel(token_per_msg=1000)
    cm = ContextManager(compact_model=fake, head_size=2, tail_size=5)
    msgs = _build_messages(100)  # 202 messages * 1000 ≈ 200K (auto threshold)

    result = await cm.compact_if_needed(msgs, fake)
    assert result.compacted is True
    assert result.reason == "auto"

    # boundary marker + summary message 가 새 sequence 에 들어 있어야 한다
    assert any(is_boundary_message(m) for m in result.messages)
    summaries = [m for m in result.messages if is_compact_summary(m)]
    assert len(summaries) == 1
    assert "compact summary body" in summaries[0].content


# ── circuit breaker: consecutive failures → skip ────────────────────────────


@pytest.mark.asyncio
async def test_circuit_breaker_skips_after_failures():
    class _Failing:
        model_name = "claude-opus-4-7"

        def get_num_tokens_from_messages(self, messages):
            return len(messages) * 1000

        async def ainvoke(self, messages):
            raise RuntimeError("api down")

    bad = _Failing()
    cm = ContextManager(
        policy=CompactPolicy(
            auto_compact_ratio=0.85,
            output_reserve_tokens=20_000,
            max_consecutive_failures=2,
            enabled_env=None,
            ratio_override_env=None,
            blocking_override_env=None,
        ),
        compact_model=bad,
        head_size=2,
        tail_size=5,
    )
    msgs = _build_messages(100)

    r1 = await cm.compact_if_needed(msgs, bad)
    assert r1.compacted is False
    assert cm.consecutive_failures == 1

    r2 = await cm.compact_if_needed(msgs, bad)
    assert cm.consecutive_failures == 2

    r3 = await cm.compact_if_needed(msgs, bad)
    assert r3.reason == "skipped:circuit_breaker"
    # 원본 보존 — 정보 손실 없음
    assert r3.messages == msgs


# ── default_policy + context window resolver wiring ─────────────────────────


def test_default_policy_and_anthropic_context_window():
    pol = default_policy()
    assert pol.auto_compact_ratio == 0.85
    assert pol.warning_ratio == 0.75
    assert pol.blocking_ratio == 0.95

    class _M:
        model_name = "claude-opus-4-7"

    assert get_context_window(_M()) == 200_000
