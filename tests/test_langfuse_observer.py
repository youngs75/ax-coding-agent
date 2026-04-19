"""LangfuseForwardObserver — SDK 호환성 회귀 방지 테스트.

Langfuse v4 SDK 는 ``client.span()`` / ``client.event()`` 를 더 이상 제공하지
않는다. 올바른 호출 경로는 ``client.start_observation()`` / ``client.create_event()``
이고, 반환된 span 은 ``update(output=...)`` + ``end()`` 로 마무리해야 한다.
SDK 이름이 다시 틀어져 매 이벤트마다 예외가 나도 메인 플로가 멈추지 않도록
silent fallback 동작도 확인한다.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from coding_agent.observability.langfuse_observer import LangfuseForwardObserver
from minyoung_mah.core.types import ObserverEvent


def _mk_event(name: str, **meta):
    return ObserverEvent(
        name=name,
        timestamp=datetime.now(),
        role="coder",
        tool=None,
        metadata=meta,
    )


class TestSDKCompatibility:
    async def test_start_event_calls_start_observation(self):
        client = MagicMock()
        obs = LangfuseForwardObserver(client=client)

        await obs.emit(_mk_event("orchestrator.role.invoke.start", run_id="r1"))

        client.start_observation.assert_called_once()
        assert client.span.called is False  # v2/legacy API must not be used
        kwargs = client.start_observation.call_args.kwargs
        assert kwargs["name"] == "orchestrator.role.invoke.start"

    async def test_end_event_updates_then_ends_span(self):
        client = MagicMock()
        span = MagicMock()
        client.start_observation.return_value = span
        obs = LangfuseForwardObserver(client=client)

        await obs.emit(_mk_event("orchestrator.role.invoke.start", run_id="r1"))
        end_evt = ObserverEvent(
            name="orchestrator.role.invoke.end",
            timestamp=datetime.now(),
            role="coder",
            duration_ms=42,
            ok=True,
            metadata={"run_id": "r1"},
        )
        await obs.emit(end_evt)

        span.update.assert_called_once()
        span.end.assert_called_once()
        # v4 end() accepts only end_time — never output=
        assert "output" not in span.end.call_args.kwargs

    async def test_orphan_end_uses_create_event(self):
        client = MagicMock()
        obs = LangfuseForwardObserver(client=client)

        end_evt = ObserverEvent(
            name="orchestrator.role.invoke.end",
            timestamp=datetime.now(),
            role="coder",
            duration_ms=5,
            ok=True,
            metadata={"run_id": "orphan"},
        )
        await obs.emit(end_evt)

        client.create_event.assert_called_once()
        assert client.event.called is False


class TestFailSilent:
    async def test_sdk_exception_does_not_propagate(self):
        client = MagicMock()
        client.start_observation.side_effect = RuntimeError("SDK drift")
        obs = LangfuseForwardObserver(client=client)

        # Must not raise.
        await obs.emit(_mk_event("orchestrator.role.invoke.start", run_id="r1"))

    async def test_no_client_is_noop(self):
        obs = LangfuseForwardObserver(client=None)
        await obs.emit(_mk_event("orchestrator.role.invoke.start", run_id="r1"))


@pytest.mark.parametrize(
    "name,meta,expected_key",
    [
        ("orchestrator.run.start", {"run_id": "abc"}, "run:abc"),
        ("orchestrator.pipeline.step.start", {"run_id": "abc", "step": 3}, "step:abc:3"),
        ("orchestrator.role.invoke.start", {"run_id": "abc"}, "role:coder:abc"),
    ],
)
def test_event_key_pairing(name, meta, expected_key):
    evt = ObserverEvent(
        name=name,
        timestamp=datetime.now(),
        role="coder",
        metadata=meta,
    )
    assert LangfuseForwardObserver._event_key(evt) == expected_key
