"""Retry Policy — 실패 분류 및 재시도 정책.

에러를 유형별로 분류하고 각 유형에 맞는 백오프 재시도 정책을 적용한다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Coroutine

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# FailureType enum
# ---------------------------------------------------------------------------

class FailureType(Enum):
    """에이전트 루프에서 발생 가능한 실패 유형."""

    MODEL_TIMEOUT = auto()
    REPEATED_STALL = auto()
    BAD_TOOL_CALL = auto()
    SUBAGENT_FAILURE = auto()
    EXTERNAL_API_ERROR = auto()
    MODEL_FALLBACK = auto()
    SAFE_STOP = auto()


# ---------------------------------------------------------------------------
# FailurePolicy dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FailurePolicy:
    """특정 실패 유형에 대한 재시도 정책.

    Parameters
    ----------
    failure_type:
        대상 실패 유형.
    max_retries:
        최대 재시도 횟수.
    backoff_base:
        지수 백오프 기본 값(초).
    backoff_max:
        최대 백오프 대기 시간(초).
    fallback_enabled:
        모델 폴백 허용 여부.
    """

    failure_type: FailureType
    max_retries: int = 0
    backoff_base: float = 1.0
    backoff_max: float = 10.0
    fallback_enabled: bool = False


# ---------------------------------------------------------------------------
# DEFAULT_POLICIES
# ---------------------------------------------------------------------------

DEFAULT_POLICIES: dict[FailureType, FailurePolicy] = {
    FailureType.MODEL_TIMEOUT: FailurePolicy(
        failure_type=FailureType.MODEL_TIMEOUT,
        max_retries=2,
        backoff_base=2.0,
        backoff_max=10.0,
        fallback_enabled=True,
    ),
    FailureType.REPEATED_STALL: FailurePolicy(
        failure_type=FailureType.REPEATED_STALL,
        max_retries=0,
        backoff_base=1.0,
        backoff_max=10.0,
        fallback_enabled=False,
    ),
    FailureType.BAD_TOOL_CALL: FailurePolicy(
        failure_type=FailureType.BAD_TOOL_CALL,
        max_retries=1,
        backoff_base=1.0,
        backoff_max=10.0,
        fallback_enabled=False,
    ),
    FailureType.SUBAGENT_FAILURE: FailurePolicy(
        failure_type=FailureType.SUBAGENT_FAILURE,
        max_retries=1,
        backoff_base=2.0,
        backoff_max=10.0,
        fallback_enabled=True,
    ),
    FailureType.EXTERNAL_API_ERROR: FailurePolicy(
        failure_type=FailureType.EXTERNAL_API_ERROR,
        max_retries=3,
        backoff_base=2.0,
        backoff_max=30.0,
        fallback_enabled=False,
    ),
    FailureType.MODEL_FALLBACK: FailurePolicy(
        failure_type=FailureType.MODEL_FALLBACK,
        max_retries=0,
        backoff_base=1.0,
        backoff_max=10.0,
        fallback_enabled=True,
    ),
    FailureType.SAFE_STOP: FailurePolicy(
        failure_type=FailureType.SAFE_STOP,
        max_retries=0,
        backoff_base=1.0,
        backoff_max=10.0,
        fallback_enabled=False,
    ),
}


# ---------------------------------------------------------------------------
# ErrorClassifier
# ---------------------------------------------------------------------------

class ErrorClassifier:
    """예외를 ``FailureType``으로 분류한다."""

    @staticmethod
    def classify(error: Exception) -> FailureType:
        """예외를 분석하여 적절한 ``FailureType``을 반환한다.

        Parameters
        ----------
        error:
            분류할 예외 인스턴스.

        Returns
        -------
        FailureType
            분류된 실패 유형.
        """
        # 1. asyncio.TimeoutError → MODEL_TIMEOUT
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            return FailureType.MODEL_TIMEOUT

        # 2. HTTP 상태 코드 기반 분류 (다양한 API 클라이언트 라이브러리 대응)
        status_code = _extract_status_code(error)
        if status_code is not None:
            if status_code == 429:
                return FailureType.EXTERNAL_API_ERROR
            if status_code >= 500:
                return FailureType.EXTERNAL_API_ERROR

        # 3. ValueError with "tool" → BAD_TOOL_CALL
        if isinstance(error, ValueError):
            msg = str(error).lower()
            if "tool" in msg:
                return FailureType.BAD_TOOL_CALL

        # 4. Generic fallback
        return FailureType.MODEL_TIMEOUT


def _extract_status_code(error: Exception) -> int | None:
    """다양한 API 에러 객체에서 HTTP 상태 코드를 추출한다."""
    # 속성 이름 우선순위: status_code, status, code, http_status
    for attr in ("status_code", "status", "http_status", "code"):
        val = getattr(error, attr, None)
        if isinstance(val, int):
            return val

    # response 객체 내부 확인
    response = getattr(error, "response", None)
    if response is not None:
        for attr in ("status_code", "status"):
            val = getattr(response, attr, None)
            if isinstance(val, int):
                return val

    return None


# ---------------------------------------------------------------------------
# retry_with_backoff
# ---------------------------------------------------------------------------

async def retry_with_backoff(
    coro_factory: Callable[[], Coroutine[Any, Any, Any]],
    policy: FailurePolicy,
) -> Any:
    """정책에 따라 지수 백오프로 코루틴을 재시도한다.

    Parameters
    ----------
    coro_factory:
        매 시도마다 새 코루틴을 생성하는 팩토리 함수.
    policy:
        적용할 재시도 정책.

    Returns
    -------
    Any
        코루틴의 성공 반환값.

    Raises
    ------
    Exception
        모든 재시도가 소진된 후 마지막 예외를 전파.
    """
    last_error: Exception | None = None

    for attempt in range(policy.max_retries + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_error = exc
            if attempt >= policy.max_retries:
                logger.error(
                    "retry.exhausted",
                    failure_type=policy.failure_type.name,
                    attempt=attempt + 1,
                    max_retries=policy.max_retries,
                    error=str(exc),
                )
                break

            delay = min(
                policy.backoff_base * (2 ** attempt),
                policy.backoff_max,
            )
            logger.warning(
                "retry.attempt",
                failure_type=policy.failure_type.name,
                attempt=attempt + 1,
                max_retries=policy.max_retries,
                delay_sec=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)

    assert last_error is not None  # noqa: S101 — 도달 불가 방어 코드
    raise last_error
