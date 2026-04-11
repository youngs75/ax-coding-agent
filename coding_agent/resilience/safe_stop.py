"""Safe Stop — 에이전트 안전 정지 조건 평가.

위험한 작업이나 한계 초과 시 에이전트를 안전하게 중단시킨다.
"""

from __future__ import annotations

from typing import Callable

import structlog

logger = structlog.get_logger(__name__)

# 보호 대상 경로 패턴 (파일 작업 시 차단)
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
    """에이전트 루프의 안전 정지 조건을 관리하고 평가한다.

    초기화 시 기본 조건이 등록되며, ``add_condition``으로 추가할 수 있다.
    """

    def __init__(self) -> None:
        self._conditions: list[tuple[str, Callable[[dict], bool], str]] = []

        # 기본 조건 등록
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
        """안전 정지 조건을 추가한다.

        Parameters
        ----------
        name:
            조건 식별 이름.
        check_fn:
            에이전트 상태 dict를 받아 정지 여부를 반환하는 함수.
        reason:
            조건 충족 시 표시할 사유.
        """
        self._conditions.append((name, check_fn, reason))
        logger.debug("safe_stop.condition_added", name=name)

    def evaluate(self, state: dict) -> tuple[bool, str]:
        """등록된 모든 조건을 상태에 대해 평가한다.

        Parameters
        ----------
        state:
            현재 에이전트 상태 딕셔너리.

        Returns
        -------
        tuple[bool, str]
            ``(should_stop, reason)``. 모든 조건 통과 시 ``(False, "")``.
        """
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
                # 조건 평가 자체에서 에러 발생 시 안전 측 판단 — 정지
                error_reason = f"조건 '{name}' 평가 중 오류 발생: {exc}"
                logger.error("safe_stop.check_error", condition=name, error=str(exc))
                return True, error_reason

        return False, ""


# ---------------------------------------------------------------------------
# Built-in condition helpers
# ---------------------------------------------------------------------------

def _check_dangerous_path(state: dict) -> bool:
    """상태에서 파일 작업 대상 경로가 보호 대상인지 확인한다."""
    # 다양한 키에서 파일 경로를 추출
    paths_to_check: list[str] = []

    # tool_args 에서 경로 관련 키 수집
    tool_args = state.get("tool_args", {})
    if isinstance(tool_args, dict):
        for key in ("path", "file_path", "file", "target", "destination", "filename"):
            val = tool_args.get(key)
            if isinstance(val, str):
                paths_to_check.append(val)

    # file_operations 리스트
    file_ops = state.get("file_operations", [])
    if isinstance(file_ops, list):
        for op in file_ops:
            if isinstance(op, dict):
                for key in ("path", "file_path", "target"):
                    val = op.get(key)
                    if isinstance(val, str):
                        paths_to_check.append(val)

    # 현재 도구 호출의 경로
    current_path = state.get("current_file_path")
    if isinstance(current_path, str):
        paths_to_check.append(current_path)

    # 위험 경로 매칭
    for path in paths_to_check:
        normalized = path.replace("\\", "/")
        for dangerous in _DANGEROUS_PATHS:
            dangerous_norm = dangerous.replace("\\", "/")
            # 경로 자체이거나 경로의 일부인 경우
            if dangerous_norm in normalized or normalized.endswith(dangerous_norm.rstrip("/")):
                return True

    return False
