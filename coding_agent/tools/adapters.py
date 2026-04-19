"""ToolAdapter 프로토콜 어댑터 — LangChain StructuredTool → minyoung_mah.ToolAdapter.

Phase 4 refactor: file_ops.py / shell.py 의 기존 LangChain 도구를 그대로 두고
(LangGraph 최상위 agent_node 가 여전히 StructuredTool 을 사용함), 동일 구현을
``minyoung_mah.ToolAdapter`` 프로토콜 준수체로 감싸 SubAgent Orchestrator 용 도구
레지스트리에 등록할 수 있게 한다.

사용 예 (Phase 5 에서 Orchestrator 조립 시)::

    from minyoung_mah import ToolRegistry
    from coding_agent.tools.adapters import FILE_ADAPTERS, SHELL_ADAPTERS

    registry = ToolRegistry()
    for a in (*FILE_ADAPTERS, *SHELL_ADAPTERS):
        registry.register(a)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import BaseModel

from minyoung_mah.core.types import ErrorCategory, ToolResult

from coding_agent.tools.file_ops import FILE_TOOLS
from coding_agent.tools.shell import SHELL_TOOLS


class _LangChainToolAdapter:
    """Wrap a LangChain ``StructuredTool`` as a ``minyoung_mah.ToolAdapter``.

    The wrapped tool's ``.invoke`` is synchronous (blocking file / shell I/O),
    so we offload it to a thread via ``asyncio.to_thread`` to respect the
    protocol's async contract. Any unexpected exception is captured as
    ``ToolResult(ok=False, ...)`` — the library contract forbids raising for
    expected failures.
    """

    def __init__(self, lc_tool: Any) -> None:
        self._tool = lc_tool
        self.name: str = lc_tool.name
        self.description: str = lc_tool.description or ""
        # StructuredTool exposes args_schema as the pydantic BaseModel subclass.
        self.arg_schema: type[BaseModel] = lc_tool.args_schema

    async def call(self, args: BaseModel) -> ToolResult:
        t0 = time.monotonic()
        try:
            payload = args.model_dump()
            result = await asyncio.to_thread(self._tool.invoke, payload)
            duration_ms = int((time.monotonic() - t0) * 1000)
            # StructuredTool already returns a str (or dict); keep as-is.
            return ToolResult(ok=True, value=result, duration_ms=duration_ms)
        except Exception as exc:  # noqa: BLE001
            duration_ms = int((time.monotonic() - t0) * 1000)
            return ToolResult(
                ok=False,
                value=None,
                error=f"{type(exc).__name__}: {exc}",
                error_category=ErrorCategory.TOOL_ERROR,
                duration_ms=duration_ms,
            )


FILE_ADAPTERS: list[_LangChainToolAdapter] = [
    _LangChainToolAdapter(t) for t in FILE_TOOLS
]
SHELL_ADAPTERS: list[_LangChainToolAdapter] = [
    _LangChainToolAdapter(t) for t in SHELL_TOOLS
]


def build_todo_adapters(
    store: Any,
    on_change: Any = None,
) -> list[_LangChainToolAdapter]:
    """Wrap write_todos/update_todo as ToolAdapters bound to *store*.

    The ledger SubAgent role receives these via the shared ToolRegistry — the
    orchestrator no longer binds them at the top level.
    """
    from coding_agent.tools.todo_tool import (
        build_update_todo_tool,
        build_write_todos_tool,
    )

    write_tool = build_write_todos_tool(store, on_change=on_change)
    update_tool = build_update_todo_tool(store, on_change=on_change)
    return [_LangChainToolAdapter(write_tool), _LangChainToolAdapter(update_tool)]


__all__ = [
    "FILE_ADAPTERS",
    "SHELL_ADAPTERS",
    "build_todo_adapters",
]
