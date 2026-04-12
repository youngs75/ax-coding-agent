"""Progress Guard — 에이전트 루프 진전 감시.

동일 도구 호출의 반복(stall)을 탐지하여 무한 루프를 방지한다.
"""

from __future__ import annotations

import re
from collections import Counter, deque
from enum import Enum, auto

import structlog

# Match a TASK-NN prefix anywhere in the description so the guard can
# count "TASK-04: implement", "TASK-04: 검증", "TASK-04 fix" as the
# same delegation target. Identical to the regex used by task_tool but
# duplicated here to avoid an import cycle.
_TASK_ID_PATTERN = re.compile(r"\bTASK-\d{2,}\b", re.IGNORECASE)

logger = structlog.get_logger(__name__)


class GuardVerdict(Enum):
    """ProgressGuard 판정 결과."""

    OK = auto()
    WARN = auto()
    STOP = auto()


class ProgressGuard:
    """에이전트의 도구 호출 패턴을 분석하여 정체 여부를 판단한다.

    Parameters
    ----------
    window_size:
        최근 행동 기록 윈도우 크기.
    stall_threshold:
        윈도우 내 동일 행동 빈도가 이 값 이상이면 정체로 판정.
    max_iterations:
        절대 반복 상한.
    """

    def __init__(
        self,
        window_size: int = 10,
        stall_threshold: int = 3,
        max_iterations: int = 50,
        task_window_size: int = 12,
        task_repeat_threshold: int = 6,
    ) -> None:
        self.window_size = window_size
        self.stall_threshold = stall_threshold
        self.max_iterations = max_iterations
        self.task_window_size = task_window_size
        self.task_repeat_threshold = task_repeat_threshold

        self._action_history: deque[tuple[str, int]] = deque(maxlen=window_size)
        self._warn_issued: bool = False
        # A-2: orchestrator-level repeated TASK-NN delegation tracking.
        # Indexed by normalised TASK-NN id; verifier↔fixer↔verifier loops
        # show up here as the same id repeating > task_repeat_threshold
        # times within the window even though the descriptions differ.
        self._task_history: deque[str] = deque(maxlen=task_window_size)
        self._task_warn_issued: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_action(self, tool_name: str, tool_args: dict) -> None:
        """도구 호출을 기록한다.

        Parameters
        ----------
        tool_name:
            호출된 도구 이름.
        tool_args:
            도구에 전달된 인자 딕셔너리.
        """
        args_hash = _safe_hash(tool_args)
        self._action_history.append((tool_name, args_hash))
        logger.debug(
            "progress_guard.record",
            tool_name=tool_name,
            args_hash=args_hash,
            history_len=len(self._action_history),
        )
        # A-2: also track repeated delegations to the same TASK-NN id.
        # Description-based deduping in _safe_hash misses verifier↔fixer
        # cycles because each iteration writes a slightly different prose.
        if tool_name == "task" and isinstance(tool_args, dict):
            desc = tool_args.get("description") or ""
            if isinstance(desc, str):
                m = _TASK_ID_PATTERN.search(desc)
                if m is not None:
                    self._task_history.append(m.group(0).upper())

    def check(self, iteration: int) -> GuardVerdict:
        """현재 상태를 판정한다.

        Parameters
        ----------
        iteration:
            현재 루프 반복 횟수.

        Returns
        -------
        GuardVerdict
            OK / WARN / STOP 판정.
        """
        # 1. 절대 반복 상한 초과
        if iteration >= self.max_iterations:
            logger.warning(
                "progress_guard.max_iterations",
                iteration=iteration,
                max_iterations=self.max_iterations,
            )
            return GuardVerdict.STOP

        # 2. 동일 TASK-NN delegation 반복 (verifier↔fixer 사이클 차단)
        if self._task_history:
            task_counter = Counter(self._task_history)
            top_task, top_freq = task_counter.most_common(1)[0]
            if top_freq >= self.task_repeat_threshold:
                if self._task_warn_issued:
                    logger.error(
                        "progress_guard.task_repeat_stop",
                        task_id=top_task,
                        frequency=top_freq,
                        threshold=self.task_repeat_threshold,
                    )
                    return GuardVerdict.STOP
                self._task_warn_issued = True
                logger.warning(
                    "progress_guard.task_repeat_warn",
                    task_id=top_task,
                    frequency=top_freq,
                    threshold=self.task_repeat_threshold,
                )
                return GuardVerdict.WARN
            # 다른 task가 진행되면 task warn 플래그 리셋
            if top_freq < self.task_repeat_threshold and self._task_warn_issued:
                self._task_warn_issued = False

        # 3. 정체 탐지 (윈도우 내 동일 행동 빈도)
        if not self._action_history:
            return GuardVerdict.OK

        counter = Counter(self._action_history)
        most_common_action, frequency = counter.most_common(1)[0]

        if frequency >= self.stall_threshold:
            if self._warn_issued:
                logger.error(
                    "progress_guard.stall_stop",
                    action=most_common_action[0],
                    frequency=frequency,
                    threshold=self.stall_threshold,
                )
                return GuardVerdict.STOP
            else:
                self._warn_issued = True
                logger.warning(
                    "progress_guard.stall_warn",
                    action=most_common_action[0],
                    frequency=frequency,
                    threshold=self.stall_threshold,
                )
                return GuardVerdict.WARN

        # 정체가 해소되면 경고 플래그 리셋
        self._warn_issued = False
        return GuardVerdict.OK

    def get_stall_summary(self) -> dict:
        """현재 기록 상태 요약을 반환한다.

        Returns
        -------
        dict
            ``history_len``, ``unique_actions``, ``most_common``,
            ``most_common_freq``, ``warn_issued`` 키를 포함하는 딕셔너리.
        """
        if not self._action_history:
            return {
                "history_len": 0,
                "unique_actions": 0,
                "most_common": None,
                "most_common_freq": 0,
                "warn_issued": self._warn_issued,
            }

        counter = Counter(self._action_history)
        most_common_action, frequency = counter.most_common(1)[0]
        return {
            "history_len": len(self._action_history),
            "unique_actions": len(counter),
            "most_common": most_common_action[0],
            "most_common_freq": frequency,
            "warn_issued": self._warn_issued,
        }

    def reset(self) -> None:
        """기록과 내부 상태를 초기화한다."""
        self._action_history.clear()
        self._warn_issued = False
        self._task_history.clear()
        self._task_warn_issued = False
        logger.debug("progress_guard.reset")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_hash(tool_args: dict) -> int:
    """중첩 dict를 포함한 인자를 안전하게 해시한다.

    ``frozenset``으로 변환 불가능한 값이 있으면 ``repr``로 폴백한다.
    """
    try:
        return hash(frozenset(tool_args.items()))
    except TypeError:
        # 중첩 dict, list 등 unhashable 값 포함 시 repr 기반 해시
        return hash(repr(sorted(tool_args.items())))
