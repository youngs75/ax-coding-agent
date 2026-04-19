"""Compatibility shim for the bits of `coding_agent.resilience` that the
minyoung-mah library does not yet cover.

What the library owns (as of v0.1.2):
  - `ProgressGuard` + `GuardVerdict` (re-exported below)
  - `ResiliencePolicy` + `default_resilience`

What stays here (ax-specific application concerns, not library):
  - `Watchdog` — asyncio coroutine timeout wrapper used in a few
    non-role paths (the library already does per-role wait_for inside
    `Orchestrator.invoke_role`).
  - `SafeStop` + `SafeStopError` + `_DANGEROUS_PATHS` — dangerous-path
    guard rules that are coding-agent-specific.
  - `ErrorHandler` + `ErrorResolution` — tier fallback policy driven from
    the top-level LangGraph `handle_error` node. The library deliberately
    stays out of tier-fallback; see plan §결정 2.
  - `ErrorClassifier` + `FailureType` + `DEFAULT_POLICIES` +
    `retry_with_backoff` — used by ErrorHandler internally.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Coroutine, Literal

import structlog

# Re-export the library's ProgressGuard + GuardVerdict so imports that
# previously read `from coding_agent.resilience import ProgressGuard, GuardVerdict`
# can migrate to `from coding_agent.resilience_compat import ...` in one step.
from minyoung_mah.resilience.progress_guard import GuardVerdict, ProgressGuard

logger = structlog.get_logger(__name__)


# =============================================================================
# Watchdog — asyncio timeout wrapper
# =============================================================================


class Watchdog:
    """코루틴 실행을 감시하는 타임아웃 워치독."""

    def __init__(self, timeout_sec: float = 30.0) -> None:
        if timeout_sec <= 0:
            raise ValueError(f"timeout_sec must be positive, got {timeout_sec}")
        self.timeout_sec = timeout_sec

    async def run(
        self,
        coro: Coroutine[Any, Any, Any],
        on_timeout: Callable[[], Any] | None = None,
    ) -> Any:
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
                if asyncio.iscoroutine(result):
                    return await result
                return result
            raise

    def __repr__(self) -> str:
        return f"Watchdog(timeout_sec={self.timeout_sec})"


# =============================================================================
# SafeStop — dangerous path + max-iter guardrails
# =============================================================================


_DANGEROUS_PATHS: tuple[str, ...] = (
    ".env",
    ".git/",
    ".git\\",
    ".ssh/",
    ".ssh\\",
    "id_rsa",
    "id_ed25519",
    ".aws/credentials",
    ".npmrc",
    ".pypirc",
)


class SafeStopError(Exception):
    """안전 정지 조건 충족으로 에이전트가 중단될 때 발생하는 예외."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"안전 정지: {reason}")


class SafeStop:
    """에이전트 루프의 안전 정지 조건을 관리하고 평가한다."""

    def __init__(self) -> None:
        self._conditions: list[tuple[str, Callable[[dict], bool], str]] = []

        self.add_condition(
            name="max_iterations",
            check_fn=lambda state: (
                state.get("iteration", 0) >= state.get("max_iterations", 50)
            ),
            reason="최대 반복 횟수에 도달했습니다.",
        )
        self.add_condition(
            name="dangerous_path",
            check_fn=_check_dangerous_path,
            reason="보호 대상 경로에 대한 파일 작업이 감지되었습니다.",
        )

    def add_condition(
        self,
        name: str,
        check_fn: Callable[[dict], bool],
        reason: str,
    ) -> None:
        self._conditions.append((name, check_fn, reason))
        logger.debug("safe_stop.condition_added", name=name)

    def evaluate(self, state: dict) -> tuple[bool, str]:
        for name, check_fn, reason in self._conditions:
            try:
                if check_fn(state):
                    logger.warning(
                        "safe_stop.triggered",
                        condition=name,
                        reason=reason,
                    )
                    return True, reason
            except Exception as exc:
                error_reason = f"조건 '{name}' 평가 중 오류 발생: {exc}"
                logger.error("safe_stop.check_error", condition=name, error=str(exc))
                return True, error_reason

        return False, ""


def _check_dangerous_path(state: dict) -> bool:
    paths_to_check: list[str] = []

    tool_args = state.get("tool_args", {})
    if isinstance(tool_args, dict):
        for key in ("path", "file_path", "file", "target", "destination", "filename"):
            val = tool_args.get(key)
            if isinstance(val, str):
                paths_to_check.append(val)

    file_ops = state.get("file_operations", [])
    if isinstance(file_ops, list):
        for op in file_ops:
            if isinstance(op, dict):
                for key in ("path", "file_path", "target"):
                    val = op.get(key)
                    if isinstance(val, str):
                        paths_to_check.append(val)

    current_path = state.get("current_file_path")
    if isinstance(current_path, str):
        paths_to_check.append(current_path)

    for path in paths_to_check:
        normalized = path.replace("\\", "/")
        for dangerous in _DANGEROUS_PATHS:
            dangerous_norm = dangerous.replace("\\", "/")
            if dangerous_norm in normalized or normalized.endswith(dangerous_norm.rstrip("/")):
                return True

    return False


# =============================================================================
# FailureType + FailurePolicy + ErrorClassifier + retry_with_backoff
# =============================================================================


class FailureType(Enum):
    """에이전트 루프에서 발생 가능한 실패 유형."""

    MODEL_TIMEOUT = auto()
    REPEATED_STALL = auto()
    BAD_TOOL_CALL = auto()
    SUBAGENT_FAILURE = auto()
    EXTERNAL_API_ERROR = auto()
    MODEL_FALLBACK = auto()
    SAFE_STOP = auto()


@dataclass(frozen=True)
class FailurePolicy:
    failure_type: FailureType
    max_retries: int = 0
    backoff_base: float = 1.0
    backoff_max: float = 10.0
    fallback_enabled: bool = False


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


class ErrorClassifier:
    """예외를 ``FailureType``으로 분류한다."""

    @staticmethod
    def classify(error: Exception) -> FailureType:
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            return FailureType.MODEL_TIMEOUT

        status_code = _extract_status_code(error)
        if status_code is not None:
            if status_code == 429:
                return FailureType.EXTERNAL_API_ERROR
            if status_code >= 500:
                return FailureType.EXTERNAL_API_ERROR

        if isinstance(error, ValueError):
            msg = str(error).lower()
            if "tool" in msg:
                return FailureType.BAD_TOOL_CALL

        return FailureType.MODEL_TIMEOUT


def _extract_status_code(error: Exception) -> int | None:
    for attr in ("status_code", "status", "http_status", "code"):
        val = getattr(error, attr, None)
        if isinstance(val, int):
            return val

    response = getattr(error, "response", None)
    if response is not None:
        for attr in ("status_code", "status"):
            val = getattr(response, attr, None)
            if isinstance(val, int):
                return val

    return None


async def retry_with_backoff(
    coro_factory: Callable[[], Coroutine[Any, Any, Any]],
    policy: FailurePolicy,
) -> Any:
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

    assert last_error is not None  # noqa: S101
    raise last_error


# =============================================================================
# ErrorHandler — tier fallback policy (plan §결정 2)
# =============================================================================


@dataclass
class ErrorResolution:
    action: Literal["retry", "fallback", "abort"]
    status_message: str
    metadata: dict[str, Any] = field(default_factory=dict)


_STATUS_MESSAGES: dict[str, dict[str, str]] = {
    "retry": {
        FailureType.MODEL_TIMEOUT.name: "모델 응답 시간 초과 — 재시도 중입니다...",
        FailureType.BAD_TOOL_CALL.name: "잘못된 도구 호출 — 수정 후 재시도합니다...",
        FailureType.SUBAGENT_FAILURE.name: "하위 에이전트 오류 — 재시도 중입니다...",
        FailureType.EXTERNAL_API_ERROR.name: "외부 API 오류 — 잠시 후 재시도합니다...",
        "_default": "오류 발생 — 재시도 중입니다...",
    },
    "fallback": {
        FailureType.MODEL_TIMEOUT.name: "모델 응답 시간 초과 — 하위 모델로 전환합니다.",
        FailureType.SUBAGENT_FAILURE.name: "하위 에이전트 오류 — 대체 모델로 전환합니다.",
        FailureType.MODEL_FALLBACK.name: "모델 전환이 필요합니다 — 하위 티어로 폴백합니다.",
        "_default": "오류 발생 — 대체 모델로 전환합니다.",
    },
    "abort": {
        FailureType.REPEATED_STALL.name: "반복 정체 감지 — 작업을 안전하게 중단합니다.",
        FailureType.SAFE_STOP.name: "안전 정지 조건 충족 — 작업을 중단합니다.",
        "_default": "복구 불가능한 오류 — 작업을 중단합니다.",
    },
}


def _get_status_message(action: str, failure_type: FailureType) -> str:
    messages = _STATUS_MESSAGES.get(action, _STATUS_MESSAGES["abort"])
    return messages.get(failure_type.name, messages["_default"])


_FALLBACK_ORDER: list[str] = ["reasoning", "strong", "default", "fast"]


def _get_next_fallback_tier(current_tier: str) -> str | None:
    try:
        idx = _FALLBACK_ORDER.index(current_tier)
    except ValueError:
        return None
    next_idx = idx + 1
    if next_idx >= len(_FALLBACK_ORDER):
        return None
    return _FALLBACK_ORDER[next_idx]


class ErrorHandler:
    """에러를 분류하고 적절한 복구 전략을 결정한다."""

    def __init__(self, fallback_enabled: bool = True) -> None:
        self.fallback_enabled = fallback_enabled

    def handle(self, error: Exception, state: dict) -> ErrorResolution:
        failure_type = ErrorClassifier.classify(error)
        policy = DEFAULT_POLICIES.get(failure_type)

        if policy is None:
            logger.error(
                "error_handler.unknown_failure_type",
                failure_type=failure_type.name,
                error=str(error),
            )
            return ErrorResolution(
                action="abort",
                status_message="알 수 없는 오류 — 작업을 중단합니다.",
                metadata={"failure_type": failure_type.name, "error": str(error)},
            )

        retry_count = state.get("retry_count_for_this_error", 0)
        current_tier = state.get("current_tier", "default")

        logger.info(
            "error_handler.classify",
            failure_type=failure_type.name,
            retry_count=retry_count,
            max_retries=policy.max_retries,
            fallback_enabled=policy.fallback_enabled and self.fallback_enabled,
            error=str(error),
        )

        if policy.max_retries > retry_count:
            return ErrorResolution(
                action="retry",
                status_message=_get_status_message("retry", failure_type),
                metadata={
                    "failure_type": failure_type.name,
                    "retry_count": retry_count + 1,
                    "max_retries": policy.max_retries,
                    "backoff_base": policy.backoff_base,
                    "backoff_max": policy.backoff_max,
                    "error": str(error),
                },
            )

        if policy.fallback_enabled and self.fallback_enabled:
            next_tier = _get_next_fallback_tier(current_tier)
            return ErrorResolution(
                action="fallback",
                status_message=_get_status_message("fallback", failure_type),
                metadata={
                    "failure_type": failure_type.name,
                    "current_tier": current_tier,
                    "next_tier": next_tier,
                    "error": str(error),
                },
            )

        return ErrorResolution(
            action="abort",
            status_message=_get_status_message("abort", failure_type),
            metadata={
                "failure_type": failure_type.name,
                "error": str(error),
            },
        )

    @staticmethod
    def format_status(resolution: ErrorResolution) -> str:
        action_icons = {
            "retry": "[재시도]",
            "fallback": "[폴백]",
            "abort": "[중단]",
        }
        icon = action_icons.get(resolution.action, "[?]")

        parts = [f"{icon} {resolution.status_message}"]

        meta = resolution.metadata
        if resolution.action == "retry" and "retry_count" in meta:
            parts.append(
                f"  시도: {meta['retry_count']}/{meta.get('max_retries', '?')}"
            )
        if resolution.action == "fallback" and "next_tier" in meta:
            parts.append(
                f"  {meta.get('current_tier', '?')} → {meta['next_tier']}"
            )
        if "failure_type" in meta:
            parts.append(f"  유형: {meta['failure_type']}")

        return "\n".join(parts)


__all__ = [
    "DEFAULT_POLICIES",
    "ErrorClassifier",
    "ErrorHandler",
    "ErrorResolution",
    "FailurePolicy",
    "FailureType",
    "GuardVerdict",
    "ProgressGuard",
    "SafeStop",
    "SafeStopError",
    "Watchdog",
    "retry_with_backoff",
]
