"""파일 조작 도구 — read, write, edit, glob, grep.

DeepAgents의 FilesystemMiddleware 패턴을 참고하여
LangChain StructuredTool로 구현한다.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path

from langchain_core.tools import tool
from pydantic import BaseModel, Field


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
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        selected = lines[offset : offset + limit]
        numbered = [f"{i + offset + 1:4d} | {line}" for i, line in enumerate(selected)]
        header = f"# {p} (lines {offset + 1}-{offset + len(selected)} of {total})"
        return header + "\n" + "\n".join(numbered)
    except Exception as e:
        return f"Error reading file: {e}"


class WriteFileInput(BaseModel):
    path: str = Field(description="생성할 파일의 경로")
    content: str = Field(description="파일에 쓸 내용")


@tool("write_file", args_schema=WriteFileInput)
def write_file(path: str, content: str) -> str:
    """새 파일을 생성하거나 기존 파일을 덮어쓴다."""
    p = Path(path).resolve()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
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
    try:
        matches = sorted(base.glob(pattern))[:100]  # 최대 100개
        if not matches:
            return f"패턴 '{pattern}'에 일치하는 파일이 없습니다."
        result = [str(m.relative_to(base)) for m in matches if m.is_file()]
        return f"# {len(result)} files found\n" + "\n".join(result)
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
        return f"패턴 '{pattern}'에 일치하는 결과가 없습니다."
    return f"# {len(results)} matches\n" + "\n".join(results)


# 전체 도구 목록
FILE_TOOLS = [read_file, write_file, edit_file, glob_files, grep]
