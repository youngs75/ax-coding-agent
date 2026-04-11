"""Agentic Loop Watchdog — 코루틴 타임아웃 감시.

장시간 실행되는 코루틴을 래핑하여 지정 시간 초과 시
콜백 호출 또는 예외를 발생시킨다.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine

import structlog

logger = structlog.get_logger(__name__)


class Watchdog:
    """코루틴 실행을 감시하는 타임아웃 워치독.

    Parameters
    ----------
    timeout_sec:
        코루틴 실행 제한 시간(초). 기본값 30초.
    """

    def __init__(self, timeout_sec: float = 30.0) -> None:
        if timeout_sec <= 0:
            raise ValueError(f"timeout_sec must be positive, got {timeout_sec}")
        self.timeout_sec = timeout_sec

    async def run(
        self,
        coro: Coroutine[Any, Any, Any],
        on_timeout: Callable[[], Any] | None = None,
    ) -> Any:
        """코루틴을 타임아웃 제한과 함께 실행한다.

        Parameters
        ----------
        coro:
            실행할 코루틴.
        on_timeout:
            타임아웃 발생 시 호출할 콜백. ``None``이면 ``asyncio.TimeoutError``를 그대로 전파.

        Returns
        -------
        Any
            코루틴의 반환값.

        Raises
        ------
        asyncio.TimeoutError
            ``on_timeout``이 제공되지 않았고 코루틴이 제한 시간을 초과한 경우.
        """
        try:
            return await asyncio.wait_for(coro, timeout=self.timeout_sec)
        except asyncio.TimeoutError:
            logger.warning(
                "watchdog.timeout",
                timeout_sec=self.timeout_sec,
                callback_provided=on_timeout is not None,
            )
            if on_timeout is not None:
                result = on_timeout()
                # 콜백이 코루틴을 반환하면 await
                if asyncio.iscoroutine(result):
                    return await result
                return result
            raise

    def __repr__(self) -> str:
        return f"Watchdog(timeout_sec={self.timeout_sec})"
