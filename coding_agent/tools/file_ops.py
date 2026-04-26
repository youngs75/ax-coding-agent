"""파일 조작 도구 — read, write, edit, glob, grep.

DeepAgents의 FilesystemMiddleware 패턴을 참고하여
LangChain StructuredTool로 구현한다.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import threading
from pathlib import Path

from langchain_core.tools import tool
from pydantic import BaseModel, Field


# ── Tool result cache ────────────────────────────────────────────────
# Cache read-only tool results within a session.  Write operations
# invalidate entries for the affected file path.

class _ToolCache:
    """Thread-safe LRU-ish cache for read-only tool results."""

    def __init__(self, max_size: int = 256) -> None:
        self._data: dict[str, str] = {}
        self._max_size = max_size
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> str | None:
        with self._lock:
            val = self._data.get(key)
            if val is not None:
                self.hits += 1
            else:
                self.misses += 1
            return val

    def put(self, key: str, value: str) -> None:
        with self._lock:
            if len(self._data) >= self._max_size:
                # Evict oldest quarter
                keys = list(self._data.keys())[: self._max_size // 4]
                for k in keys:
                    del self._data[k]
            self._data[key] = value

    def invalidate_path(self, path: str) -> None:
        """Remove all cache entries whose key contains *path*."""
        resolved = str(Path(path).resolve())
        with self._lock:
            to_del = [k for k in self._data if resolved in k]
            for k in to_del:
                del self._data[k]

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_cache = _ToolCache()


def get_tool_cache() -> _ToolCache:
    """Return the global tool cache instance (for testing/monitoring)."""
    return _cache


# ── Tool implementations ────────────────────────────────────────────

class ReadFileInput(BaseModel):
    path: str = Field(description="읽을 파일의 절대 또는 상대 경로")
    offset: int = Field(default=0, description="읽기 시작 줄 번호 (0-based)")
    limit: int = Field(default=200, description="읽을 최대 줄 수")


@tool("read_file", args_schema=ReadFileInput)
def read_file(path: str, offset: int = 0, limit: int = 200) -> str:
    """파일 내용을 읽어 반환한다. offset/limit으로 부분 읽기 가능."""
    p = Path(path).resolve()
    if not p.exists():
        return f"Error: 파일이 존재하지 않습니다: {p}"
    if not p.is_file():
        return f"Error: 디렉토리입니다. read_file은 파일만 읽을 수 있습니다: {p}"

    # Check mtime for cache validity
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0
    cache_key = f"read:{p}:{offset}:{limit}:{mtime}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        selected = lines[offset : offset + limit]
        numbered = [f"{i + offset + 1:4d} | {line}" for i, line in enumerate(selected)]
        header = f"# {p} (lines {offset + 1}-{offset + len(selected)} of {total})"
        result = header + "\n" + "\n".join(numbered)
        _cache.put(cache_key, result)
        return result
    except Exception as e:
        return f"Error reading file: {e}"


class WriteFileInput(BaseModel):
    path: str = Field(description="생성할 파일의 경로")
    content: str = Field(description="파일에 쓸 내용")


# ── Path policy: block one anti-pattern at the tool boundary ────────
# Creating *-mobile.tsx / *-desktop.tsx files for "responsive web" is
# rejected because responsive web should be one codebase + CSS media
# queries, and prior runs showed coders silently splitting components
# across files when they should have used a single responsive layout.
# This is a narrow B-form constraint kept until we revisit it.

_PLATFORM_SUFFIX_RE = re.compile(
    r"-(mobile|desktop|android|ios|tablet)\.(tsx|ts|jsx|js|vue|svelte|css|scss|styled\.ts)$",
    re.IGNORECASE,
)

# v22 #4 — TDD pre-hook: 코드 파일 작성 전 대응 테스트 파일 존재 확인.
# 환경변수 ``AX_DISABLE_TDD_HOOK=1`` 로 비활성화 (TDD 가 부적합한 프로젝트
# — 예: PoC, prototyping). Default = 활성.
_TDD_HOOK_ENABLED = os.getenv("AX_DISABLE_TDD_HOOK", "").strip() not in ("1", "true", "yes")

# TDD 적용 대상 코드 확장자. md/json/yaml/css/html/sql 등 *비실행* 파일은 예외.
_TDD_CHECKED_EXTENSIONS: set[str] = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".swift",
}

# 워크스페이스 루트 마커 — write 대상 파일에서 위로 올라가며 검색.
_WORKSPACE_ROOT_MARKERS: tuple[str, ...] = (
    ".ax-agent",
    ".git",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "DONE_CONDITION.md",
)

# 검색에서 항상 제외할 디렉토리 (vendor, build artifacts, harness 내부)
_TDD_SKIP_DIRS: set[str] = {
    ".git", ".ax-agent", "node_modules", "__pycache__", ".venv",
    "venv", "dist", "build", ".pytest_cache", "memory_store",
    ".pnpm-store", ".turbo", ".next", "target", "vendor",
}

# 파일이 *테스트 파일* 인지 식별 — 경로 어디든 이 마커가 있으면 test 로 분류.
_TEST_PATH_MARKERS: tuple[str, ...] = (
    "/tests/", "/test/", "/__tests__/", "/spec/", "/specs/",
    ".test.", ".spec.", "_test.", "_spec.",
)


def _is_test_file_path(p: Path) -> bool:
    """경로가 test/spec 파일이면 True."""
    s = str(p).replace("\\", "/").lower()
    if any(marker in s for marker in _TEST_PATH_MARKERS):
        return True
    name = p.name.lower()
    if name.startswith("test_") or name.startswith("spec_"):
        return True
    return False


def _find_workspace_root(target: Path) -> Path:
    """target 파일 위로 5단계까지 올라가며 워크스페이스 루트 탐색."""
    cur = target.parent
    for _ in range(6):
        for marker in _WORKSPACE_ROOT_MARKERS:
            if (cur / marker).exists():
                return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    # 못 찾으면 target.parent 를 root 로 간주 (보수)
    return target.parent


def _workspace_has_any_tests(root: Path) -> bool:
    """워크스페이스에 테스트 파일이 *하나라도* 존재하면 True.

    아무 테스트도 없으면 boot phase 로 보고 TDD hook 을 발동하지 않는다
    (첫 파일은 어떻게든 만들어야 하므로 닭-달걀 회피).
    """
    if not root.exists():
        return False
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(p in _TDD_SKIP_DIRS for p in path.parts):
                continue
            if _is_test_file_path(path):
                return True
    except Exception:
        pass
    return False


def _has_corresponding_test(target: Path, root: Path) -> bool:
    """target 의 *동일 모듈* 테스트 파일이 워크스페이스에 존재하는지.

    매칭 기준:
    - target 의 stem (확장자 제거한 basename) 이 어떤 test 파일 경로/이름에
      포함되어 있으면 매치.
    - 예: ``src/auth/jwt.service.ts`` → ``tests/auth/jwt.service.test.ts`` 매치.
    """
    stem = target.stem  # 'jwt.service' from 'jwt.service.ts'
    if not stem or len(stem) < 2:
        return True  # 짧은 stem 은 false-positive 위험 — 통과
    stem_lower = stem.lower()
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(p in _TDD_SKIP_DIRS for p in path.parts):
                continue
            if not _is_test_file_path(path):
                continue
            if stem_lower in str(path).lower():
                return True
    except Exception:
        # 파일시스템 오류 시 통과 (보수적)
        return True
    return False


def _check_write_policy(path_str: str, content: str = "") -> str | None:
    """Return an error message if *path_str* violates write policy, else None.

    ``content`` 는 v22 #4 의 향후 확장용 (e.g., test 파일이면 assert 가
    있는지 검사). 현재는 사용 안 함.
    """
    name = Path(path_str).name
    if _PLATFORM_SUFFIX_RE.search(name):
        return (
            f"REJECTED: 플랫폼별 파일명 패턴은 금지됩니다 ({name}). "
            "Responsive web은 단일 코드베이스 + CSS media query로 구현하세요. "
            "예: `LoginPage.tsx` 하나 + `@media (max-width: 768px)`. "
            "별도 -mobile/-desktop/-tablet 파일을 만들지 마세요."
        )

    # v22 #4 — TDD pre-hook
    if not _TDD_HOOK_ENABLED:
        return None
    target = Path(path_str)
    if target.suffix.lower() not in _TDD_CHECKED_EXTENSIONS:
        return None  # 비-코드 파일 (md/json/yaml/css/sql 등) 통과
    if _is_test_file_path(target):
        return None  # 테스트 파일 자체 통과
    root = _find_workspace_root(target)
    if not _workspace_has_any_tests(root):
        return None  # boot phase — 첫 파일은 통과
    if _has_corresponding_test(target, root):
        return None  # 대응 테스트 존재 → OK
    return (
        f"REJECTED: TDD 위반 — 코드 파일 작성 전에 테스트 파일이 필요합니다 "
        f"({target.name}). 동일 모듈의 테스트 파일 ({target.stem}.test.* / "
        f"{target.stem}.spec.* / test_{target.stem}.* 등) 이 워크스페이스에 "
        f"존재하지 않습니다. 먼저 테스트 파일을 작성한 뒤 (Red 단계), 그 "
        f"테스트를 통과시키는 코드를 작성하세요 (Green 단계). "
        f"TDD 가 부적합한 작업이면 환경변수 AX_DISABLE_TDD_HOOK=1 로 "
        f"비활성화 가능."
    )


@tool("write_file", args_schema=WriteFileInput)
def write_file(path: str, content: str) -> str:
    """새 파일을 생성하거나 기존 파일을 덮어쓴다."""
    policy_error = _check_write_policy(path, content)
    if policy_error is not None:
        return policy_error

    p = Path(path).resolve()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        _cache.invalidate_path(str(p))
        return f"파일 작성 완료: {p} ({len(content)} bytes)"
    except Exception as e:
        return f"Error writing file: {e}"


class EditFileInput(BaseModel):
    path: str = Field(description="편집할 파일의 경로")
    old_string: str = Field(description="교체할 기존 문자열 (정확히 일치해야 함)")
    new_string: str = Field(description="교체될 새 문자열")


@tool("edit_file", args_schema=EditFileInput)
def edit_file(path: str, old_string: str, new_string: str) -> str:
    """파일에서 old_string을 찾아 new_string으로 교체한다."""
    p = Path(path).resolve()
    if not p.exists():
        return f"Error: 파일이 존재하지 않습니다: {p}"
    try:
        text = p.read_text(encoding="utf-8")
        count = text.count(old_string)
        if count == 0:
            return f"Error: old_string을 파일에서 찾을 수 없습니다."
        if count > 1:
            return f"Error: old_string이 {count}번 발견되었습니다. 더 구체적인 문자열을 사용하세요."
        new_text = text.replace(old_string, new_string, 1)
        p.write_text(new_text, encoding="utf-8")
        _cache.invalidate_path(str(p))
        return f"편집 완료: {p}"
    except Exception as e:
        return f"Error editing file: {e}"


class GlobInput(BaseModel):
    pattern: str = Field(description="검색할 glob 패턴 (예: '**/*.py')")
    path: str = Field(default=".", description="검색 시작 디렉토리")


@tool("glob_files", args_schema=GlobInput)
def glob_files(pattern: str, path: str = ".") -> str:
    """glob 패턴으로 파일을 검색한다."""
    base = Path(path).resolve()
    if not base.exists():
        return f"Error: 디렉토리가 존재하지 않습니다: {base}"

    cache_key = f"glob:{base}:{pattern}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        matches = sorted(base.glob(pattern))[:100]  # 최대 100개
        if not matches:
            return f"패턴 '{pattern}'에 일치하는 파일이 없습니다."
        result_lines = [str(m.relative_to(base)) for m in matches if m.is_file()]
        result = f"# {len(result_lines)} files found\n" + "\n".join(result_lines)
        _cache.put(cache_key, result)
        return result
    except Exception as e:
        return f"Error: {e}"


class GrepInput(BaseModel):
    pattern: str = Field(description="검색할 정규식 패턴")
    path: str = Field(default=".", description="검색 대상 경로")
    include: str = Field(default="", description="포함할 파일 패턴 (예: '*.py')")


@tool("grep", args_schema=GrepInput)
def grep(pattern: str, path: str = ".", include: str = "") -> str:
    """파일 내용에서 정규식 패턴을 검색한다."""
    base = Path(path).resolve()
    if not base.exists():
        return f"Error: 경로가 존재하지 않습니다: {base}"

    cache_key = f"grep:{base}:{pattern}:{include}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    results: list[str] = []
    max_results = 50

    def search_file(fp: Path) -> None:
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                if re.search(pattern, line):
                    rel = fp.relative_to(base)
                    results.append(f"{rel}:{i}: {line.strip()}")
                    if len(results) >= max_results:
                        return
        except Exception:
            pass

    if base.is_file():
        search_file(base)
    else:
        for root, _, files in os.walk(base):
            for fname in files:
                if include and not fnmatch.fnmatch(fname, include):
                    continue
                search_file(Path(root) / fname)
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break

    if not results:
        output = f"패턴 '{pattern}'에 일치하는 결과가 없습니다."
    else:
        output = f"# {len(results)} matches\n" + "\n".join(results)

    _cache.put(cache_key, output)
    return output


# 전체 도구 목록
FILE_TOOLS = [read_file, write_file, edit_file, glob_files, grep]
