"""UserDecisionsLog — ask_user_question 답변 세션 누적 테스트.

Phase 6 refactor 에서 ``SubAgentManager._user_decisions`` 를 독립 모듈
(``coding_agent.subagents.user_decisions``) 로 분리. 이전 manager 테스트가
검증하던 의미 (중복 제거, 빈 답변 무시, header rendering) 를 새 장소에서 재확인.
"""

from __future__ import annotations

from coding_agent.subagents.user_decisions import UserDecisionsLog


def test_empty_log_has_no_items_and_empty_header():
    log = UserDecisionsLog()
    assert log.items() == []
    assert log.header() == ""


def test_record_accumulates_in_order():
    log = UserDecisionsLog()
    log.record("User answered — Tech: React")
    log.record("User answered — Mobile: 반응형 웹만")
    assert log.items() == [
        "User answered — Tech: React",
        "User answered — Mobile: 반응형 웹만",
    ]


def test_duplicate_record_is_dropped():
    log = UserDecisionsLog()
    log.record("User answered — Tech: React")
    log.record("User answered — Tech: React")
    assert len(log.items()) == 1


def test_empty_string_is_not_recorded():
    log = UserDecisionsLog()
    log.record("")
    assert log.items() == []


def test_header_renders_markdown_block():
    log = UserDecisionsLog()
    log.record("User answered — Tech: React")
    log.record("User answered — Mobile: 반응형 웹만")
    header = log.header()

    assert "## 사용자 결정 사항 (하드 제약)" in header
    assert "- User answered — Tech: React" in header
    assert "- User answered — Mobile: 반응형 웹만" in header


def test_clear_resets_to_empty():
    log = UserDecisionsLog()
    log.record("User answered — Tech: React")
    log.clear()
    assert log.items() == []
    assert log.header() == ""


def test_role_build_user_message_prepends_header():
    """roles.CodingAgentRole.build_user_message 가 UserDecisionsLog.header() 를
    task_summary 앞에 붙이는지 — Phase 5 결정 1."""
    from minyoung_mah import InvocationContext

    from coding_agent.subagents.roles import coder_role

    log = UserDecisionsLog()
    log.record("User answered — Tech: React")

    role = coder_role(user_decisions=log)
    msg = role.build_user_message(
        InvocationContext(task_summary="TASK-01: UI 구현", user_request="")
    )
    assert "User answered — Tech: React" in msg
    assert "TASK-01: UI 구현" in msg
    assert msg.index("User answered") < msg.index("TASK-01")


def test_role_build_user_message_injects_memory_snippets():
    from minyoung_mah import InvocationContext

    from coding_agent.subagents.roles import coder_role

    role = coder_role()  # no user_decisions
    msg = role.build_user_message(
        InvocationContext(
            task_summary="implement X",
            user_request="",
            memory_snippets=["<agent_memory>...</agent_memory>"],
        )
    )
    assert "<agent_memory>" in msg
    assert "implement X" in msg


def test_role_build_user_message_surfaces_previous_ask():
    from minyoung_mah import InvocationContext

    from coding_agent.subagents.roles import planner_role

    role = planner_role()
    msg = role.build_user_message(
        InvocationContext(
            task_summary="continue PRD",
            user_request="",
            parent_outputs={"previous_ask": "User answered — Tech: React"},
        )
    )
    assert "직전 사용자 답변" in msg
    assert "User answered — Tech: React" in msg
