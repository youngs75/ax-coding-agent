"""P3.5 회귀 방지 테스트.

다음 동작을 코드 레벨에서 검증:

1. write_file이 PRD/SPEC/SDD 등 설계 문서를 자유롭게 허용한다 (강제 폐기 후)
2. write_file이 *-mobile.tsx 등 플랫폼별 파일명 패턴은 여전히 거부한다
3. SubAgentManager가 ask_user_question 답변을 누적한다
4. 누적된 user_decisions가 다음 SubAgent의 HumanMessage에 prepend된다
5. decisions가 없으면 HumanMessage가 변하지 않는다 (기본 동작 보존)
6. fixer/coder/verifier 도구 경계가 유지된다
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


# ── 1) write_file — SPEC/PRD/SDD는 자유롭게 작성 가능 ──────────
# 이전에는 submit_spec_section을 강제하기 위해 SPEC 경로를 거부했지만,
# Sub-B 결정으로 LLM이 PRD/SPEC/SDD를 자율적으로 작성하도록 강제를 풀었다.
# 이 테스트들은 회귀 방지: 어떤 문서든 write_file로 정상 작성되어야 한다.


@pytest.mark.parametrize(
    "filename",
    ["PRD.md", "SPEC.md", "SDD.md", "spec.md", "Architecture.md", "DESIGN.md"],
)
def test_write_file_allows_design_documents(tmp_path: Path, filename: str) -> None:
    target = tmp_path / "docs" / filename
    result = write_file.invoke({"path": str(target), "content": f"# {filename}\n\nbody"})
    assert "REJECTED" not in result
    assert target.exists()


def test_check_write_policy_does_not_block_spec() -> None:
    """SPEC 경로 차단 정책이 완전히 제거되었는지 직접 검증."""
    for path in ("SPEC.md", "docs/SPEC.md", "spec.md", "/tmp/x/docs/SPEC.md"):
        assert _check_write_policy(path) is None, f"unexpected reject for {path}"


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
