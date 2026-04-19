"""Session-scoped log of ``ask_user_question`` answers.

Every SubAgent role's ``build_user_message`` prepends these decisions so the
planner, coder, verifier, and fixer all see the same hard constraints the
user gave (replaces the ``_user_decisions`` + ``_decisions_header`` helpers
that used to live on ``SubAgentManager``).
"""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


class UserDecisionsLog:
    """Append-only log of formatted user decisions (one per ask/answer pair).

    Held at AgentLoop level and passed into both ``ask_user_question``
    (which writes) and the role factories via ``build_orchestrator`` (which
    reads through each role's ``build_user_message``).
    """

    def __init__(self) -> None:
        self._items: list[str] = []

    def record(self, formatted_answer: str) -> None:
        if formatted_answer and formatted_answer not in self._items:
            self._items.append(formatted_answer)
            log.info(
                "user_decisions.recorded",
                count=len(self._items),
                preview=formatted_answer[:80],
            )

    def items(self) -> list[str]:
        return list(self._items)

    def header(self) -> str:
        """Markdown block to prepend to every SubAgent task description."""
        if not self._items:
            return ""
        lines = ["## 사용자 결정 사항 (하드 제약)"]
        for d in self._items:
            lines.append(f"- {d}")
        lines.append("")
        lines.append("")
        return "\n".join(lines)

    def clear(self) -> None:
        self._items.clear()
