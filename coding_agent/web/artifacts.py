"""Workspace 다운로드 endpoint — `/artifacts/__bundle.zip` + 개별 파일.

ax 가 코드 작성한 workspace 를 HTTP 로 회수한다. zip 으로 묶어 streaming
다운로드 (전체) 또는 path 단위 단일 파일. apt-web 의
``/coding/a2a/artifacts/{path:path}`` proxy 가 이 endpoint 로 relay.

Workspace download — full zip stream + single-file download. Pulled by
apt-web's `/coding/a2a/artifacts/{path:path}` proxy.

제외:
- VCS 메타: ``.git``
- 패키지 캐시: ``node_modules``, ``__pycache__``, ``.venv``, ``.next`` 등
- 빌드 산출물: ``dist``, ``build``, ``target``
- secrets: ``.env``
- 컴파일 산출물: ``*.pyc``, ``*.pyo``

사용자가 IDE 에서 열기 좋은 *순수 source* 만 zip 에 포함.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path
from typing import Iterator

import structlog
from fastapi import HTTPException
from fastapi.responses import FileResponse, StreamingResponse

log = structlog.get_logger()


# 제외할 디렉토리 이름. path 의 *어떤 segment* 라도 매칭되면 skip.
# Excluded dir names — matched against any segment in path.
_EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".next",
    ".nuxt",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".turbo",
    "target",  # rust
    ".idea",
    ".vscode",
})

# 제외할 *파일명* 또는 정확히 매칭되는 path. .env 는 secrets 보호.
# Excluded specific filenames (secrets, etc).
_EXCLUDE_FILES: frozenset[str] = frozenset({".env", ".env.local"})

# 제외할 파일 suffix.
_EXCLUDE_SUFFIX: frozenset[str] = frozenset({".pyc", ".pyo"})

# 전체 bundle path. apt-web router 의 ``{path:path}`` 가 이걸 보내면 zip 응답.
# Reserved path that triggers full-zip response when reached via the
# `{path:path}` catch-all route.
_BUNDLE_NAME = "__bundle.zip"


def _resolve_workspace() -> Path:
    """ax 가 코드 작성한 workspace 디렉토리 경로.

    우선순위: ``AX_ARTIFACTS_DIR`` env > ``config.project_root``.

    Workspace path. AX_ARTIFACTS_DIR overrides config.project_root.
    """
    artifacts_env = os.environ.get("AX_ARTIFACTS_DIR")
    if artifacts_env:
        return Path(artifacts_env).resolve()
    # Lazy import — config 가 langgraph 등 무거운 모듈 안 끌어옴.
    from coding_agent.config import get_config
    return get_config().project_root.resolve()


def _is_excluded_path(p: Path) -> bool:
    """경로 어디든 exclude rule 에 매칭되면 True."""
    if any(part in _EXCLUDE_DIRS for part in p.parts):
        return True
    if p.name in _EXCLUDE_FILES:
        return True
    if p.suffix in _EXCLUDE_SUFFIX:
        return True
    return False


def _walk_files(root: Path) -> Iterator[Path]:
    """Yield 포함할 *파일* 만 (디렉토리 제외, exclude rule 적용)."""
    if not root.exists() or not root.is_dir():
        return
    for p in root.rglob("*"):
        if _is_excluded_path(p):
            continue
        if p.is_file():
            yield p


async def stream_workspace_bundle() -> StreamingResponse:
    """전체 workspace 를 zip 으로 streaming 다운로드.

    Stream the workspace as a zip download. In-memory build (작업 산출물은
    보통 < 50MB). 큰 워크스페이스면 tempfile 백업으로 변경 검토.
    """
    workspace = _resolve_workspace()
    if not workspace.exists() or not workspace.is_dir():
        raise HTTPException(404, f"workspace not found: {workspace}")

    log.info("artifacts.bundle.start", workspace=str(workspace))
    buf = io.BytesIO()
    file_count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in _walk_files(workspace):
            arcname = p.relative_to(workspace).as_posix()
            try:
                zf.write(p, arcname)
                file_count += 1
            except (OSError, PermissionError) as exc:
                # 일부 파일 read 실패는 skip — 전체 다운로드 막지 말 것.
                log.warning("artifacts.bundle.skip", path=str(p), error=str(exc))
    buf.seek(0)
    size = len(buf.getvalue())
    log.info("artifacts.bundle.ready", file_count=file_count, size_bytes=size)

    def gen() -> Iterator[bytes]:
        # 64KB chunk streaming.
        while True:
            chunk = buf.read(64 * 1024)
            if not chunk:
                break
            yield chunk

    return StreamingResponse(
        gen(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="ax-workspace.zip"',
            "Content-Length": str(size),
            "X-Artifacts-File-Count": str(file_count),
        },
    )


async def serve_workspace_file(path: str) -> FileResponse:
    """workspace 내 단일 파일 다운로드 (path traversal 방어 포함).

    Single file download with path-traversal protection.
    """
    workspace = _resolve_workspace()
    if not workspace.exists() or not workspace.is_dir():
        raise HTTPException(404, "workspace not found")

    target = (workspace / path).resolve()
    # workspace 밖으로 나가는 path 거부 (../etc/passwd 등).
    try:
        target.relative_to(workspace.resolve())
    except ValueError:
        raise HTTPException(403, "path outside workspace")

    if target.is_dir():
        # 디렉토리 통째 다운로드는 __bundle.zip 만 허용.
        raise HTTPException(400, "directory download not supported; use /__bundle.zip")

    if not target.is_file():
        raise HTTPException(404, f"file not found: {path}")

    # Exclude rule 에 걸리는 파일도 거부 — secrets / vendored 보호.
    if _is_excluded_path(target.relative_to(workspace.resolve())):
        raise HTTPException(403, "path under excluded directory")

    return FileResponse(
        target,
        media_type="application/octet-stream",
        filename=target.name,
    )


__all__ = [
    "stream_workspace_bundle",
    "serve_workspace_file",
    "_resolve_workspace",
    "_walk_files",
    "_is_excluded_path",
    "_EXCLUDE_DIRS",
    "_EXCLUDE_FILES",
    "_EXCLUDE_SUFFIX",
    "_BUNDLE_NAME",
]
