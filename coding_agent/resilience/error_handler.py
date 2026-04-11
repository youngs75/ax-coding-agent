"""Error Handler — 통합 에러 처리 및 복구 전략 결정.

에러를 분류하고 재시도, 폴백, 중단 중 적절한 조치를 결정한다.
사용자 표시 메시지는 한국어로 제공한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from coding_agent.resilience.retry_policy import (
    DEFAULT_POLICIES,
    ErrorClassifier,
    FailureType,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# ErrorResolution dataclass
# ---------------------------------------------------------------------------

@dataclass
class ErrorResolution:
    """에러 처리 결과.

    Parameters
    ----------
    action:
        수행할 조치 — ``"retry"`` / ``"fallback"`` / ``"abort"``.
    status_message:
        사용자에게 표시할 상태 메시지 (한국어).
    metadata:
        추가 메타데이터 (폴백 티어, 재시도 횟수 등).
    """

    action: Literal["retry", "fallback", "abort"]
    status_message: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Korean status messages
# ---------------------------------------------------------------------------

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
    """조치와 실패 유형에 맞는 한국어 상태 메시지를 반환한다."""
    messages = _STATUS_MESSAGES.get(action, _STATUS_MESSAGES["abort"])
    return messages.get(failure_type.name, messages["_default"])


# ---------------------------------------------------------------------------
# ErrorHandler
# ---------------------------------------------------------------------------

class ErrorHandler:
    """에러를 분류하고 적절한 복구 전략을 결정한다.

    Parameters
    ----------
    fallback_enabled:
        전역 폴백 허용 여부. ``False``이면 정책의 ``fallback_enabled``와 무관하게
        폴백을 수행하지 않는다.
    """

    def __init__(self, fallback_enabled: bool = True) -> None:
        self.fallback_enabled = fallback_enabled

    def handle(self, error: Exception, state: dict) -> ErrorResolution:
        """에러를 분석하고 복구 전략을 결정한다.

        Parameters
        ----------
        error:
            발생한 예외.
        state:
            현재 에이전트 상태 딕셔너리.

        Returns
        -------
        ErrorResolution
            결정된 복구 전략.
        """
        failure_type = ErrorClassifier.classify(error)
        policy = DEFAULT_POLICIES.get(failure_type)

        # 알 수 없는 실패 유형에 대한 방어
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

        # 1. 재시도 가능한 경우
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

        # 2. 폴백 가능한 경우
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

        # 3. 중단
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
        """``ErrorResolution``을 CLI 표시용 문자열로 포매팅한다.

        Parameters
        ----------
        resolution:
            포매팅할 에러 해결 정보.

        Returns
        -------
        str
            사용자에게 표시할 포매팅된 문자열.
        """
        action_icons = {
            "retry": "[재시도]",
            "fallback": "[폴백]",
            "abort": "[중단]",
        }
        icon = action_icons.get(resolution.action, "[?]")

        parts = [f"{icon} {resolution.status_message}"]

        # 메타데이터 기반 추가 정보
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# 모델 티어 폴백 순서 (models.py FALLBACK_ORDER 와 동기화)
_FALLBACK_ORDER: list[str] = ["reasoning", "strong", "default", "fast"]


def _get_next_fallback_tier(current_tier: str) -> str | None:
    """현재 티어보다 한 단계 낮은 폴백 티어를 반환한다."""
    try:
        idx = _FALLBACK_ORDER.index(current_tier)
    except ValueError:
        return None
    next_idx = idx + 1
    if next_idx >= len(_FALLBACK_ORDER):
        return None
    return _FALLBACK_ORDER[next_idx]
