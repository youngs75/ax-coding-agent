"""Tests for coding_agent.tools.spec_tool — structured SPEC submission."""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.tools.spec_tool import (
    SpecSectionStore,
    build_submit_spec_section_tool,
    validate_section_content,
)


# ── Fixture data ─────────────────────────────────────────────────────

GOOD_GOALS = (
    "The product is a project management system with Gantt charts.\n"
    "Target users are small engineering teams.\n\n"
    "- Deliver create/read/update/delete for projects\n"
    "- Deliver a drag-resize Gantt view\n"
    "- Deliver responsive layouts on desktop and tablet\n"
    "- Ship with automated tests covering the API surface\n"
)

def _build_good_task_block(i: int) -> str:
    return (
        f"- TASK-{i:02d}: Implement feature {i}\n"
        f"  - **산출물**: src/feature_{i}/module.py, tests/test_feature_{i}.py\n"
        f"  - **GWT**\n"
        f"    - G: feature {i} 의 초기 상태 준비됨\n"
        f"    - W: feature_{i}.run() 호출\n"
        f"    - T: 결과가 기대값과 일치하고 실패 케이스는 예외 발생\n"
    )


GOOD_TASKS = (
    "Atomic tasks required to ship the MVP.\n\n"
    + "\n".join(_build_good_task_block(i) for i in range(1, 11))
)

GOOD_DEPS = (
    "- TASK-01 -> TASK-02\n"
    "- TASK-02 -> TASK-03\n"
    "- TASK-04 depends on TASK-03\n"
    "- TASK-05 -> TASK-06\n"
)

GOOD_DOD = "\n".join(f"- [ ] Criterion {i}" for i in range(1, 30))


# ── validate_section_content ────────────────────────────────────────


def test_goals_rejects_short_content():
    with pytest.raises(ValueError, match="at least 200"):
        validate_section_content("goals", "too short")


def test_goals_rejects_missing_bullets():
    content = "a" * 250  # long enough but no bullets
    with pytest.raises(ValueError, match="bullet"):
        validate_section_content("goals", content)


def test_goals_accepts_well_formed():
    validate_section_content("goals", GOOD_GOALS)


def test_tasks_rejects_too_few_ids():
    # Build a long-enough section that still has too few unique TASK ids.
    content = "Tasks:\n" + "".join(
        _build_good_task_block(i) for i in range(1, 4)
    ) + ("x" * 1500)
    with pytest.raises(ValueError, match="at least 10"):
        validate_section_content("tasks", content)


def test_tasks_counts_unique_ids_only():
    # Repeating TASK-01 ten times should NOT satisfy the ≥10 requirement.
    content = (_build_good_task_block(1) * 12) + ("x" * 400)
    with pytest.raises(ValueError, match="at least 10"):
        validate_section_content("tasks", content)


def test_tasks_rejects_missing_artifact_in_block():
    # 10 tasks present, long enough, acceptance criteria present,
    # but NO artifact/file reference → must be rejected.
    blocks = []
    for i in range(1, 11):
        blocks.append(
            f"- TASK-{i:02d}: doing feature {i} with some care and attention\n"
            f"  - acceptance criteria: should work as described above\n"
            f"  - G: initial state\n  - W: action taken\n  - T: expected outcome\n"
        )
    content = "tasks: " + "\n".join(blocks)
    with pytest.raises(ValueError, match="artifact"):
        validate_section_content("tasks", content)


def test_tasks_rejects_missing_acceptance_in_block():
    # 10 tasks present, artifact refs present, but no GWT/acceptance → reject.
    blocks = []
    for i in range(1, 11):
        blocks.append(
            f"- TASK-{i:02d}: doing feature {i} with extra description here\n"
            f"  - **산출물**: src/feature_{i}/module.py, tests/test_feature_{i}.py\n"
            f"  - more notes about the implementation and its internals...\n"
        )
    content = "tasks: " + "\n".join(blocks)
    with pytest.raises(ValueError, match="acceptance"):
        validate_section_content("tasks", content)


def test_tasks_accepts_well_formed():
    validate_section_content("tasks", GOOD_TASKS)


def test_dependencies_rejects_when_no_edges():
    with pytest.raises(ValueError, match="dependency edge"):
        validate_section_content("dependencies", "- these are some tasks")


def test_dependencies_accepts_arrow_and_text_forms():
    validate_section_content("dependencies", GOOD_DEPS)


def test_dod_rejects_few_checkboxes():
    with pytest.raises(ValueError, match="at least 25"):
        validate_section_content("dod", "- [ ] only one")


def test_dod_accepts_full_checklist():
    validate_section_content("dod", GOOD_DOD)


# ── SpecSectionStore ────────────────────────────────────────────────


def test_store_tracks_submissions_until_complete(tmp_path: Path):
    store = SpecSectionStore(spec_path=tmp_path / "docs/SPEC.md")

    msg = store.submit("goals", GOOD_GOALS)
    assert "1/4" in msg
    assert not store.is_complete()

    store.submit("tasks", GOOD_TASKS)
    store.submit("dependencies", GOOD_DEPS)
    final = store.submit("dod", GOOD_DOD)

    assert store.is_complete()
    assert "ALL_SECTIONS_SUBMITTED" in final
    target = tmp_path / "docs/SPEC.md"
    assert target.exists()
    body = target.read_text(encoding="utf-8")
    assert "## 1. Goals" in body
    assert "## 2. Atomic Tasks" in body
    assert "## 3. Dependencies" in body
    assert "## 4. Definition of Done" in body
    assert "TASK-05" in body


def test_store_is_idempotent_on_extra_complete_calls(tmp_path: Path):
    store = SpecSectionStore(spec_path=tmp_path / "SPEC.md")
    store.submit("goals", GOOD_GOALS)
    store.submit("tasks", GOOD_TASKS)
    store.submit("dependencies", GOOD_DEPS)
    first = store.submit("dod", GOOD_DOD)
    second = store.submit("dod", GOOD_DOD)  # overwrite same section
    assert "ALL_SECTIONS_SUBMITTED" in first
    assert "ALL_SECTIONS_SUBMITTED" in second


# ── StructuredTool wrapper ──────────────────────────────────────────


def test_tool_rejects_weak_content_with_friendly_message(tmp_path: Path):
    store = SpecSectionStore(spec_path=tmp_path / "SPEC.md")
    tool = build_submit_spec_section_tool(store)

    reply = tool.invoke({"section": "goals", "content": "too short"})
    assert reply.startswith("REJECTED (goals)")
    assert store.submitted_sections() == []


def test_tool_happy_path_writes_spec(tmp_path: Path):
    store = SpecSectionStore(spec_path=tmp_path / "docs/SPEC.md")
    tool = build_submit_spec_section_tool(store)

    for section, body in [
        ("goals", GOOD_GOALS),
        ("tasks", GOOD_TASKS),
        ("dependencies", GOOD_DEPS),
        ("dod", GOOD_DOD),
    ]:
        reply = tool.invoke({"section": section, "content": body})
        assert "REJECTED" not in reply

    assert store.is_complete()
    assert (tmp_path / "docs/SPEC.md").exists()


# ── Manager wiring ──────────────────────────────────────────────────


def test_manager_resolver_returns_fresh_store_per_call():
    """Each SubAgent invocation must get an isolated SpecSectionStore."""
    from unittest.mock import MagicMock

    from coding_agent.subagents.factory import SubAgentFactory
    from coding_agent.subagents.manager import SubAgentManager
    from coding_agent.subagents.registry import SubAgentRegistry

    manager = SubAgentManager(SubAgentRegistry(), SubAgentFactory(SubAgentRegistry(), MagicMock()))
    first = manager._resolve_tools(["submit_spec_section"])
    second = manager._resolve_tools(["submit_spec_section"])

    assert len(first) == 1 and len(second) == 1
    s1 = first[0].metadata["spec_store"]
    s2 = second[0].metadata["spec_store"]
    assert s1 is not s2

    s1.submit("goals", GOOD_GOALS)
    assert s1.submitted_sections() == ["goals"]
    assert s2.submitted_sections() == []  # not leaked


def test_planner_role_includes_submit_spec_section():
    from coding_agent.subagents.factory import ROLE_TEMPLATES

    assert "submit_spec_section" in ROLE_TEMPLATES["planner"].default_tools
