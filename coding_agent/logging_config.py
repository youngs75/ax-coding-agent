"""로깅 설정 — 콘솔은 깨끗하게, 디버그 로그는 파일로.

일반 사용자 모드:
    콘솔 → WARNING 이상만 출력
    파일 → .ax-agent/logs/agent.log에 DEBUG 전체 기록

개발자 모드 (AX_DEBUG=1):
    콘솔 → DEBUG 전체 출력
    파일 → 동일
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import structlog


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

    # 파일 핸들러: 항상 DEBUG 전체
    if log_file:
        file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
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
        logger_factory=structlog.PrintLoggerFactory(
            file=open(str(log_file), "a", encoding="utf-8", buffering=1) if log_file else sys.stderr
        ),
        cache_logger_on_first_use=True,
    )

    return log_file
