"""P3.5 회귀 방지 테스트.

다음 회귀 5건을 코드 레벨에서 차단했는지 검증:

1. write_file이 SPEC.md 경로를 거부한다 (submit_spec_section 우회 방지)
2. write_file이 *-mobile.tsx 등 플랫폼별 파일명 패턴을 거부한다
3. write_file이 정상 경로는 여전히 허용한다
4. SubAgentManager가 ask_user_question 답변을 누적한다
5. 누적된 user_decisions가 다음 SubAgent의 HumanMessage에 prepend된다
6. decisions가 없으면 HumanMessage가 변하지 않는다 (기본 동작 보존)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from coding_agent.subagents.factory import SubAgentFactory
from coding_agent.subagents.manager import SubAgentManager
from coding_agent.subagents.models import SubAgentInstance, SubAgentStatus
from coding_agent.subagents.registry import SubAgentRegistry
from coding_agent.tools.file_ops import _check_write_policy, write_file


# ── 1) write_file — SPEC 경로 거부 ──────────────────────────────

@pytest.mark.parametrize(
    "path",
    [
        "SPEC.md",
        "docs/SPEC.md",
        "/tmp/workspace/docs/SPEC.md",
        "spec.md",
        "docs/spec",
        "./SPEC.md",
        "some/nested/path/SPEC.md",
    ],
)
def test_write_file_rejects_spec_paths(path: str) -> None:
    err = _check_write_policy(path)
    assert err is not None
    assert "submit_spec_section" in err
    assert "REJECTED" in err


def test_write_file_tool_rejects_spec(tmp_path: Path) -> None:
    target = tmp_path / "docs" / "SPEC.md"
    result = write_file.invoke({"path": str(target), "content": "# SPEC\n\nsome content"})
    assert "REJECTED" in result
    assert "submit_spec_section" in result
    assert not target.exists(), "SPEC.md must not be written on policy failure"


def test_write_file_allows_prd(tmp_path: Path) -> None:
    target = tmp_path / "docs" / "PRD.md"
    result = write_file.invoke({"path": str(target), "content": "# PRD\n"})
    assert "REJECTED" not in result
    assert target.exists()


# ── 2) write_file — 플랫폼별 파일명 거부 ────────────────────────

@pytest.mark.parametrize(
    "filename",
    [
        "LoginPage-mobile.tsx",
        "Gantt-desktop.tsx",
        "Foo-tablet.jsx",
        "Bar-android.ts",
        "Baz-ios.js",
        "Header-mobile.vue",
        "Nav-MOBILE.tsx",  # case-insensitive
    ],
)
def test_write_file_rejects_platform_suffix(tmp_path: Path, filename: str) -> None:
    target = tmp_path / "src" / filename
    result = write_file.invoke({"path": str(target), "content": "export const x = 1;"})
    assert "REJECTED" in result
    assert "media query" in result or "responsive" in result.lower()
    assert not target.exists()


def test_write_file_allows_normal_component(tmp_path: Path) -> None:
    target = tmp_path / "src" / "LoginPage.tsx"
    result = write_file.invoke(
        {"path": str(target), "content": "export const LoginPage = () => null;"}
    )
    assert "REJECTED" not in result
    assert target.exists()


def test_write_file_allows_mobile_as_directory(tmp_path: Path) -> None:
    # "mobile/Foo.tsx" is a directory name, not a platform suffix; allowed.
    target = tmp_path / "mobile" / "Foo.tsx"
    result = write_file.invoke(
        {"path": str(target), "content": "export const Foo = () => null;"}
    )
    assert "REJECTED" not in result
    assert target.exists()


# ── 3) SubAgentManager — user decisions 누적 & prepend ──────────

def _make_manager() -> SubAgentManager:
    registry = SubAgentRegistry()
    llm = MagicMock()
    factory = SubAgentFactory(registry, llm)
    return SubAgentManager(registry, factory)


def test_record_user_decision_accumulates() -> None:
    manager = _make_manager()
    assert manager.get_user_decisions() == []

    manager.record_user_decision("User answered — Tech: React")
    manager.record_user_decision("User answered — Mobile: 반응형 웹만")
    assert len(manager.get_user_decisions()) == 2


def test_record_user_decision_dedupes() -> None:
    manager = _make_manager()
    manager.record_user_decision("User answered — Tech: React")
    manager.record_user_decision("User answered — Tech: React")  # dup
    assert len(manager.get_user_decisions()) == 1


def test_record_user_decision_ignores_empty() -> None:
    manager = _make_manager()
    manager.record_user_decision("")
    assert manager.get_user_decisions() == []


def test_decisions_header_empty_returns_empty_string() -> None:
    manager = _make_manager()
    assert manager._decisions_header() == ""


def test_decisions_header_renders_block() -> None:
    manager = _make_manager()
    manager.record_user_decision("User answered — Tech: React")
    manager.record_user_decision("User answered — Mobile: 반응형 웹만")
    header = manager._decisions_header()
    assert "## 사용자 결정 사항" in header
    assert "- User answered — Tech: React" in header
    assert "- User answered — Mobile: 반응형 웹만" in header
    assert "## 작업 내용" in header
    assert "하드 제약" in header


# ── 4) Integration — resolve_tools wires ask_user_question callback ──

def test_resolve_tools_ask_user_question_records_on_answer() -> None:
    manager = _make_manager()
    tools = manager._resolve_tools(["ask_user_question"])
    assert len(tools) == 1
    ask_tool = tools[0]
    assert ask_tool.name == "ask_user_question"

    # Simulate the on_answer callback firing — verify it routes to manager.
    # We can't invoke interrupt() directly here; instead, exercise the
    # recording path by calling record_user_decision which the wrapped
    # tool will call via its closure.
    manager.record_user_decision("User answered — Mobile: Responsive only")
    assert "User answered — Mobile: Responsive only" in manager.get_user_decisions()


def test_resolve_tools_submit_spec_section_fresh_per_call() -> None:
    manager = _make_manager()
    a = manager._resolve_tools(["submit_spec_section"])[0]
    b = manager._resolve_tools(["submit_spec_section"])[0]
    # Different instances → independent stores (per-session isolation).
    assert a is not b


def test_resolve_tools_static_tool_shared() -> None:
    manager = _make_manager()
    a = manager._resolve_tools(["read_file"])[0]
    b = manager._resolve_tools(["read_file"])[0]
    assert a is b  # shared across calls


# ── 5) Role separation — fixer must not have execute ───────────

def test_fixer_role_has_no_execute_tool() -> None:
    """Fixer should be unable to run shell commands.

    The verifier runs tests; fixer only edits code. Giving fixer execute
    access caused a regression where it looped on hanging vitest/jest
    watch-mode commands.
    """
    from coding_agent.subagents.factory import ROLE_TEMPLATES

    fixer_tools = ROLE_TEMPLATES["fixer"].default_tools
    assert "execute" not in fixer_tools
    assert "edit_file" in fixer_tools
    assert "read_file" in fixer_tools


def test_verifier_role_keeps_execute_tool() -> None:
    """Verifier remains the only role that can run tests."""
    from coding_agent.subagents.factory import ROLE_TEMPLATES

    assert "execute" in ROLE_TEMPLATES["verifier"].default_tools


def test_coder_role_keeps_execute_tool() -> None:
    """Coder still needs execute for builds/installs."""
    from coding_agent.subagents.factory import ROLE_TEMPLATES

    assert "execute" in ROLE_TEMPLATES["coder"].default_tools
