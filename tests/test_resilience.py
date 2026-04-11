"""복원력 시스템 테스트 — timeout, retry, progress guard, safe stop."""

from __future__ import annotations

import asyncio

import pytest

from coding_agent.resilience.error_handler import ErrorHandler, ErrorResolution
from coding_agent.resilience.progress_guard import GuardVerdict, ProgressGuard
from coding_agent.resilience.retry_policy import (
    ErrorClassifier,
    FailureType,
    DEFAULT_POLICIES,
)
from coding_agent.resilience.safe_stop import SafeStop, SafeStopError
from coding_agent.resilience.watchdog import Watchdog


class TestWatchdog:
    @pytest.mark.asyncio
    async def test_normal_execution(self):
        wd = Watchdog(timeout_sec=5.0)

        async def fast():
            return 42

        result = await wd.run(fast())
        assert result == 42

    @pytest.mark.asyncio
    async def test_timeout(self):
        wd = Watchdog(timeout_sec=0.1)

        async def slow():
            await asyncio.sleep(10)
            return 42

        with pytest.raises(asyncio.TimeoutError):
            await wd.run(slow())

    @pytest.mark.asyncio
    async def test_timeout_with_callback(self):
        wd = Watchdog(timeout_sec=0.1)
        callback_called = False

        async def on_timeout():
            nonlocal callback_called
            callback_called = True
            return "fallback"

        async def slow():
            await asyncio.sleep(10)

        result = await wd.run(slow(), on_timeout=on_timeout)
        assert callback_called
        assert result == "fallback"


class TestErrorClassifier:
    def test_timeout_error(self):
        assert ErrorClassifier.classify(asyncio.TimeoutError()) == FailureType.MODEL_TIMEOUT

    def test_value_error_with_tool(self):
        assert ErrorClassifier.classify(ValueError("invalid tool call")) == FailureType.BAD_TOOL_CALL

    def test_generic_error(self):
        result = ErrorClassifier.classify(RuntimeError("something"))
        assert result == FailureType.MODEL_TIMEOUT  # 기본 폴백


class TestProgressGuard:
    def test_ok_on_normal(self):
        pg = ProgressGuard(window_size=5, stall_threshold=3, max_iterations=50)
        pg.record_action("read_file", {"path": "/a.py"})
        pg.record_action("write_file", {"path": "/b.py"})

        assert pg.check(2) == GuardVerdict.OK

    def test_warn_on_stall(self):
        pg = ProgressGuard(window_size=5, stall_threshold=3, max_iterations=50)
        # 같은 액션 3번 반복
        for _ in range(3):
            pg.record_action("read_file", {"path": "/same.py"})

        verdict = pg.check(3)
        assert verdict in (GuardVerdict.WARN, GuardVerdict.STOP)

    def test_stop_on_max_iterations(self):
        pg = ProgressGuard(max_iterations=10)
        assert pg.check(10) == GuardVerdict.STOP
        assert pg.check(11) == GuardVerdict.STOP

    def test_reset(self):
        pg = ProgressGuard(window_size=5, stall_threshold=3, max_iterations=50)
        for _ in range(3):
            pg.record_action("read_file", {"path": "/same.py"})
        pg.reset()
        assert pg.check(0) == GuardVerdict.OK

    def test_stall_summary(self):
        pg = ProgressGuard()
        pg.record_action("read_file", {"path": "/a.py"})
        pg.record_action("write_file", {"path": "/b.py"})

        summary = pg.get_stall_summary()
        assert "history_len" in summary


class TestSafeStop:
    def test_no_stop_normal(self):
        ss = SafeStop()
        state = {"iteration": 5, "max_iterations": 50}
        should_stop, reason = ss.evaluate(state)
        assert not should_stop

    def test_stop_max_iterations(self):
        ss = SafeStop()
        state = {"iteration": 50, "max_iterations": 50}
        should_stop, reason = ss.evaluate(state)
        assert should_stop

    def test_stop_dangerous_path(self):
        ss = SafeStop()
        state = {
            "iteration": 1,
            "max_iterations": 50,
            "tool_args": {"path": "/home/user/.env"},
        }
        should_stop, reason = ss.evaluate(state)
        assert should_stop

    def test_custom_condition(self):
        ss = SafeStop()
        ss.add_condition(
            "test_condition",
            lambda s: s.get("custom_flag", False),
            "custom flag triggered",
        )
        state = {"iteration": 1, "max_iterations": 50, "custom_flag": True}
        should_stop, reason = ss.evaluate(state)
        assert should_stop
        assert "custom" in reason.lower()


class TestErrorHandler:
    def test_retry_decision(self):
        handler = ErrorHandler(fallback_enabled=True)
        state = {"retry_count_for_this_error": 0, "current_tier": "strong"}
        resolution = handler.handle(asyncio.TimeoutError(), state)
        assert resolution.action == "retry"

    def test_fallback_after_retries(self):
        handler = ErrorHandler(fallback_enabled=True)
        state = {"retry_count_for_this_error": 3, "current_tier": "strong"}
        resolution = handler.handle(asyncio.TimeoutError(), state)
        assert resolution.action == "fallback"
        assert resolution.metadata.get("next_tier") == "default"

    def test_abort_when_no_fallback(self):
        handler = ErrorHandler(fallback_enabled=False)
        state = {"retry_count_for_this_error": 3, "current_tier": "fast"}
        resolution = handler.handle(asyncio.TimeoutError(), state)
        assert resolution.action == "abort"

    def test_format_status(self):
        resolution = ErrorResolution(
            action="retry",
            status_message="재시도 중",
            metadata={"retry_count": 1, "max_retries": 2},
        )
        formatted = ErrorHandler.format_status(resolution)
        assert "[재시도]" in formatted

    def test_korean_status_messages(self):
        handler = ErrorHandler()
        state = {"retry_count_for_this_error": 0, "current_tier": "strong"}
        resolution = handler.handle(asyncio.TimeoutError(), state)
        # 한국어 메시지 확인
        assert any(c >= '\uac00' for c in resolution.status_message)


class TestFailurePolicies:
    def test_all_types_have_policies(self):
        for ft in FailureType:
            assert ft in DEFAULT_POLICIES, f"Missing policy for {ft.name}"

    def test_safe_stop_no_retry(self):
        policy = DEFAULT_POLICIES[FailureType.SAFE_STOP]
        assert policy.max_retries == 0
        assert not policy.fallback_enabled

    def test_model_timeout_has_fallback(self):
        policy = DEFAULT_POLICIES[FailureType.MODEL_TIMEOUT]
        assert policy.fallback_enabled
        assert policy.max_retries >= 1
