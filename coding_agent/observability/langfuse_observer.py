"""Langfuse forwarder — minyoung_mah.Observer 구현.

Orchestrator 가 내뿜는 ``orchestrator.role.invoke.*`` / ``orchestrator.pipeline.*``
이벤트를 Langfuse span 으로 매핑한다. LiteLLM 통합이 이미 LLM 호출을 trace/
generation 으로 기록하므로, 이 observer 는 그 상위의 role-level span 을 덧붙이는
역할. Langfuse 환경변수가 설정되지 않았거나 SDK 초기화가 실패하면 silently
no-op 한다.

환경변수 (이미 .env 에 있어야 함):
  LANGFUSE_PUBLIC_KEY
  LANGFUSE_SECRET_KEY
  LANGFUSE_HOST (기본: https://cloud.langfuse.com)
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import structlog

from minyoung_mah import (
    CompositeObserver,
    Observer,
    StructlogObserver,
)

if TYPE_CHECKING:
    from minyoung_mah.core.types import ObserverEvent

log = structlog.get_logger(__name__)


def _build_langfuse_client() -> Any | None:
    """Best-effort Langfuse 클라이언트 초기화. 실패 시 ``None``."""
    if not os.getenv("LANGFUSE_PUBLIC_KEY") or not os.getenv("LANGFUSE_SECRET_KEY"):
        return None
    try:
        from langfuse import Langfuse  # type: ignore[import-not-found]

        return Langfuse()
    except Exception as exc:  # noqa: BLE001
        log.warning("langfuse_observer.init_failed", error=str(exc))
        return None


class LangfuseForwardObserver:
    """Forward orchestrator events to Langfuse as spans (best-effort)."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client or _build_langfuse_client()
        # Active spans keyed by run_id / step / role_invocation for pairing
        # `.start` 와 `.end` 이벤트.
        self._spans: dict[str, Any] = {}

    async def emit(self, event: "ObserverEvent") -> None:
        if self._client is None:
            return
        try:
            self._handle(event)
        except Exception as exc:  # noqa: BLE001
            # Observer failures must never break the main flow.
            # Keep at debug to avoid log flooding when SDK API drifts.
            log.debug("langfuse_observer.emit_failed", name=event.name, error=str(exc))

    def _handle(self, event: "ObserverEvent") -> None:
        name = event.name
        meta = event.metadata or {}
        key = self._event_key(event)

        if name.endswith(".start"):
            span = self._client.start_observation(
                name=name,
                input=meta,
                metadata={"role": event.role, "tool": event.tool},
            )
            if key is not None:
                self._spans[key] = span
        elif name.endswith(".end"):
            span = self._spans.pop(key, None) if key else None
            if span is None:
                # No matching start — record a point-in-time event instead.
                self._client.create_event(
                    name=name,
                    metadata={
                        "role": event.role,
                        "tool": event.tool,
                        "ok": event.ok,
                        "duration_ms": event.duration_ms,
                        **meta,
                    },
                )
                return
            try:
                span.update(
                    output={"ok": event.ok, "duration_ms": event.duration_ms, **meta},
                )
                span.end()
            except Exception as exc:  # noqa: BLE001
                log.debug("langfuse_observer.span_end_failed", name=name, error=str(exc))

    @staticmethod
    def _event_key(event: "ObserverEvent") -> str | None:
        """Pair start/end by whichever ids the library provides."""
        meta = event.metadata or {}
        run_id = meta.get("run_id")
        step = meta.get("step")
        if event.name.startswith("orchestrator.run."):
            return f"run:{run_id}" if run_id else None
        if event.name.startswith("orchestrator.pipeline.step"):
            return f"step:{run_id}:{step}" if run_id and step else None
        if event.name.startswith("orchestrator.role.invoke"):
            return f"role:{event.role}:{run_id or step or ''}"
        return None


def build_default_observer() -> Observer:
    """Return the composite observer ax uses by default.

    Always includes StructlogObserver. Adds LangfuseForwardObserver when
    the Langfuse env keys are present; otherwise only structlog.
    """
    structlog_obs = StructlogObserver()
    lf = LangfuseForwardObserver()
    if lf._client is None:
        return structlog_obs
    return CompositeObserver(structlog_obs, lf)


__all__ = [
    "LangfuseForwardObserver",
    "build_default_observer",
]
