"""로깅 설정 — 콘솔은 깨끗하게, 디버그 로그는 파일로.

일반 사용자 모드:
    콘솔 → WARNING 이상만 출력
    파일 → .ax-agent/logs/agent.log에 DEBUG 전체 기록

개발자 모드 (AX_DEBUG=1):
    콘솔 → DEBUG 전체 출력
    파일 → 동일
"""

from __future__ import annotations

import atexit
import logging
import os
import sys
from pathlib import Path

import structlog


class _FlushingFileHandler(logging.FileHandler):
    """FileHandler that flushes after every emit AND fsyncs on close.

    stdlib FileHandler already calls ``self.flush()`` in ``emit()`` (via
    ``StreamHandler.emit``), but the flush only pushes bytes to the OS — it
    does not guarantee they hit disk. If the process hangs (e.g. a blocking
    observer call in the same thread) before the next I/O scheduling tick,
    tail -f readers can see the log appear frozen at an earlier point.
    This subclass adds ``os.fsync`` on close and an ``atexit`` hook so that
    even abrupt shutdowns flush to disk.
    """

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        # StreamHandler.emit already calls self.flush(); keep the call as
        # a no-op safeguard in case future refactors drop it.
        try:
            if self.stream is not None:
                self.stream.flush()
        except (OSError, ValueError):
            pass


def setup_logging(workspace: str | None = None) -> Path | None:
    """로깅을 설정한다. 로그 파일 경로를 반환."""
    debug_mode = os.getenv("AX_DEBUG", "").strip() in ("1", "true", "yes")

    # 로그 디렉토리: workspace/.ax-agent/logs/
    log_dir = None
    log_file = None
    ws = workspace or os.getcwd()

    try:
        log_dir = Path(ws) / ".ax-agent" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "agent.log"
    except OSError:
        log_dir = None
        log_file = None

    # ── LiteLLM 로깅 억제 ──
    if not debug_mode:
        logging.getLogger("LiteLLM").setLevel(logging.WARNING)
        logging.getLogger("litellm").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("openai").setLevel(logging.WARNING)

        import litellm
        litellm.suppress_debug_info = True
        litellm.set_verbose = False

    # ── stdlib logging 설정 ──
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if debug_mode else logging.WARNING)

    # 기존 핸들러 제거
    for h in root_logger.handlers[:]:
        root_logger.removeHandler(h)

    # 콘솔 핸들러: 디버그 모드면 DEBUG, 아니면 WARNING만
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.DEBUG if debug_mode else logging.WARNING)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(console_handler)

    # 파일 핸들러: 항상 DEBUG 전체. _FlushingFileHandler 로 defense-in-depth.
    file_handler: _FlushingFileHandler | None = None
    if log_file:
        file_handler = _FlushingFileHandler(str(log_file), encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
        )
        root_logger.addHandler(file_handler)

    # ── structlog 설정 ──
    # structlog의 필터 레벨:
    #   - 파일이 있으면 DEBUG (모든 timing/info 로그를 파일에 기록)
    #   - 파일이 없으면 콘솔 모드에 따라 결정
    structlog_level = logging.DEBUG if log_file else (logging.DEBUG if debug_mode else logging.WARNING)

    structlog_file = (
        open(str(log_file), "a", encoding="utf-8", buffering=1)
        if log_file
        else sys.stderr
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.KeyValueRenderer(
                key_order=["event", "timestamp", "level"],
            ),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(structlog_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=structlog_file),
        cache_logger_on_first_use=True,
    )

    # atexit: normal interpreter shutdown already flushes handlers via
    # ``logging.shutdown``, but we explicitly fsync the FileHandler stream
    # and flush/close the structlog file so data survives even when a
    # blocking call (e.g. observer cleanup) hangs near exit.
    def _final_flush() -> None:
        try:
            logging.shutdown()
        except Exception:
            pass
        if file_handler is not None:
            try:
                if file_handler.stream is not None:
                    file_handler.stream.flush()
                    try:
                        os.fsync(file_handler.stream.fileno())
                    except (OSError, ValueError, AttributeError):
                        pass
            except Exception:
                pass
        if log_file is not None and structlog_file is not sys.stderr:
            try:
                structlog_file.flush()
                try:
                    os.fsync(structlog_file.fileno())
                except (OSError, ValueError, AttributeError):
                    pass
            except Exception:
                pass

    atexit.register(_final_flush)

    return log_file
