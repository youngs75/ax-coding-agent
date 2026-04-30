"""SSE event mapping regression tests for ``coding_agent.web.sse_emitter``.

Two regressions covered (2026-04-28 portal integration e2e):

1. **write_todos inside a SubAgent was suppressed** — the noise filter that
   silences events while ``subagent_depth > 0`` ran *before* the
   ``write_todos / update_todo → orchestrator.todo.change`` translation, so
   the planner's ``write_todos`` call (which always runs inside its task
   loop) never reached the chat UI's todo panel.

2. **``ask_user_question`` payload mismatch** — the CLI-canonical shape is
   ``{questions: [{question, options: [{label, description}], allow_other}]}``,
   but the SSE emitter previously only read the legacy flat
   ``{question, choices, allow_free_text}`` form, so ``input_required`` SSE
   went out with the default question and an empty choices list. The chat
   UI's HITL modal therefore had no question and no buttons.

apt-web chat UI 가 받는 SSE event 매핑 회귀 차단. 위 두 회귀가 같은 e2e turn
(planner 가 todo 분해 → decomposition_gate 사용자 confirm 대기) 에서 동시에
나와 chat 화면이 read_file 단계에서 freeze 됐던 것.
"""

from __future__ import annotations

from coding_agent.web.sse_emitter import (
    _input_required_payload,
    _map_langgraph_event,
)


# ── write_todos inside SubAgent ───────────────────────────────────────────


def test_write_todos_inside_subagent_still_emits_todo_change() -> None:
    """planner 안의 write_todos 도 todo.change 로 emit 돼야 한다.

    SubAgent 억제 분기가 write_todos 처리보다 먼저 걸려 todo panel 이
    비어있던 회귀 (2026-04-28 portal e2e).
    """
    state = {"subagent_depth": 1, "subagent_started_at": 0.0, "last_role": "planner"}
    output = [
        {"id": "TASK-1", "content": "FastAPI 앱 작성", "status": "pending"},
        {"id": "TASK-2", "content": "pytest 5개 작성", "status": "pending"},
    ]
    frame = _map_langgraph_event(
        "on_tool_end", "write_todos", {"output": output}, state
    )
    assert frame is not None
    text = frame.decode("utf-8")
    assert "orchestrator.todo.change" in text
    assert "TASK-1" in text
    assert "TASK-2" in text


def test_update_todo_inside_subagent_emits_todo_change() -> None:
    """update_todo 도 같은 우회 경로로 todo.change emit."""
    state = {"subagent_depth": 1, "subagent_started_at": 0.0, "last_role": "coder"}
    output = [{"id": "TASK-1", "content": "FastAPI 앱", "status": "in_progress"}]
    frame = _map_langgraph_event(
        "on_tool_end", "update_todo", {"output": output}, state
    )
    assert frame is not None
    assert b"orchestrator.todo.change" in frame
    assert b"in_progress" in frame


def test_write_todos_start_inside_subagent_is_silent() -> None:
    """write_todos *start* 는 emit 안 함 (end 의 todos 추출만 사용)."""
    state = {"subagent_depth": 1, "subagent_started_at": 0.0, "last_role": "planner"}
    frame = _map_langgraph_event(
        "on_tool_start", "write_todos", {"input": {}}, state
    )
    assert frame is None


def test_write_todos_string_output_uses_store_snapshot() -> None:
    """Production write_todos returns a *summary string* (render_todo_summary),
    not a structured list. Without a TodoStore reference the mapper used to
    fall back to JSON parsing of that string, fail, and silently drop every
    todo.change event — the chat UI's todo panel stayed empty for the entire
    portal e2e (2026-04-30).

    실제 production 의 write_todos 가 string 을 반환할 때, store snapshot 으로
    todos 를 surface 해야 한다. 회귀 차단.
    """

    class _StubItem:
        def __init__(self, id: str, content: str, status: str) -> None:
            self.id, self.content, self.status = id, content, status

    class _StubStore:
        def list_items(self) -> list[_StubItem]:
            return [
                _StubItem("TASK-1", "FastAPI 앱 작성", "in_progress"),
                _StubItem("TASK-2", "pytest 5개 작성", "pending"),
            ]

    state = {
        "subagent_depth": 1,
        "subagent_started_at": 0.0,
        "last_role": "planner",
        "todo_store": _StubStore(),
    }
    # Simulate the *real* tool ToolMessage payload — a human-readable summary,
    # not a JSON list. Previously this caused _extract_todos → None → emit skip.
    summary_string = (
        "Todos: 2 total — pending=1, in_progress=1, completed=0.\n"
        "  [~] TASK-1: FastAPI 앱 작성\n"
        "  [ ] TASK-2: pytest 5개 작성"
    )
    frame = _map_langgraph_event(
        "on_tool_end", "write_todos", {"output": summary_string}, state
    )
    assert frame is not None, (
        "write_todos with string summary output must still emit todo.change "
        "via the TodoStore snapshot (regression: portal e2e 2026-04-30)."
    )
    text = frame.decode("utf-8")
    assert "orchestrator.todo.change" in text
    assert "TASK-1" in text
    assert "TASK-2" in text
    assert "in_progress" in text


def test_write_todos_string_output_without_store_skips_emit() -> None:
    """No store + string output → skip cleanly (no false positive frame).

    Defensive: if AgentLoop ever ships without ``get_todo_store`` we should
    skip rather than emit a malformed frame.
    """
    state = {"subagent_depth": 1, "subagent_started_at": 0.0, "last_role": "planner"}
    frame = _map_langgraph_event(
        "on_tool_end",
        "write_todos",
        {"output": "Todos: 1 total — pending=1, in_progress=0, completed=0."},
        state,
    )
    assert frame is None


def test_update_todo_string_output_uses_store_snapshot() -> None:
    """update_todo 도 같은 store-우선 경로를 따라야 한다."""

    class _StubItem:
        def __init__(self, id: str, content: str, status: str) -> None:
            self.id, self.content, self.status = id, content, status

    class _StubStore:
        def list_items(self) -> list[_StubItem]:
            return [_StubItem("TASK-1", "FastAPI 앱", "completed")]

    state = {
        "subagent_depth": 1,
        "subagent_started_at": 0.0,
        "last_role": "coder",
        "todo_store": _StubStore(),
    }
    frame = _map_langgraph_event(
        "on_tool_end",
        "update_todo",
        {"output": "Updated TASK-1 → completed."},
        state,
    )
    assert frame is not None
    assert b"orchestrator.todo.change" in frame
    assert b"completed" in frame


def test_normal_tool_inside_subagent_still_suppressed() -> None:
    """기존 동작 보존 — SubAgent 안의 일반 tool 은 여전히 SSE 노이즈로 억제."""
    state = {"subagent_depth": 1, "subagent_started_at": 0.0, "last_role": "planner"}
    frame = _map_langgraph_event(
        "on_tool_start", "read_file", {"input": {"path": "/x"}}, state
    )
    assert frame is None


def test_top_level_tool_call_still_emits_role_tool_call_start() -> None:
    """orchestrator-direct tool 호출은 기존대로 role.tool.call.start emit."""
    state = {"subagent_depth": 0, "subagent_started_at": 0.0, "last_role": "auto"}
    frame = _map_langgraph_event(
        "on_tool_start", "read_file", {"input": {"path": "/x.py"}}, state
    )
    assert frame is not None
    assert b"role.tool.call.start" in frame
    assert b"read_file" in frame


# ── role.invoke role tracking ─────────────────────────────────────────────


def test_role_invoke_end_reports_same_role_as_start() -> None:
    """task start 의 agent_type 이 invoke.end 까지 같은 값으로 흘러야 한다.

    Regression (2026-04-29): start 분기에서 ``last_role`` 저장 누락으로
    invoke.end 가 항상 초기값 ``"auto"`` 를 보내 chat UI 에
    ``: planner / : auto`` 두 줄로 표시되던 회귀.
    """
    state = {"subagent_depth": 0, "subagent_started_at": 0.0, "last_role": "auto"}

    start_frame = _map_langgraph_event(
        "on_tool_start",
        "task",
        {"input": {"agent_type": "planner", "description": "분해해줘"}},
        state,
    )
    assert start_frame is not None
    assert b"orchestrator.role.invoke.start" in start_frame
    assert b'"role": "planner"' in start_frame
    assert state["last_role"] == "planner"

    end_frame = _map_langgraph_event(
        "on_tool_end",
        "task",
        {"output": "COMPLETED"},
        state,
    )
    assert end_frame is not None
    assert b"orchestrator.role.invoke.end" in end_frame
    assert b'"role": "planner"' in end_frame


# ── input_required payload extraction ──────────────────────────────────────


def test_input_required_payload_cli_questions_form() -> None:
    """CLI ``questions[].options`` payload 가 apt-web 형태로 정확히 변환.

    ``_build_decomposition_interrupt_payload`` 와 ``ask_user_question`` tool
    이 만드는 형태. 이 변환이 누락되어 modal 이 빈 payload 로 떨어지던 회귀.
    """
    payload = {
        "kind": "ask_user_question",
        "questions": [
            {
                "header": "분해 확인",
                "question": "어떻게 진행할까요?",
                "multi_select": False,
                "allow_other": False,
                "options": [
                    {"label": "이대로 진행", "description": "원안 그대로"},
                    {"label": "더 세분화", "description": "재분해"},
                    {"label": "더 통합", "description": "통합"},
                ],
            }
        ],
    }
    result = _input_required_payload(payload, task_id="t-1")

    assert result["session_id"] == "t-1"
    assert result["question"] == "어떻게 진행할까요?"
    assert result["choices"] == [
        {"id": "이대로 진행", "label": "이대로 진행", "description": "원안 그대로"},
        {"id": "더 세분화", "label": "더 세분화", "description": "재분해"},
        {"id": "더 통합", "label": "더 통합", "description": "통합"},
    ]
    assert result["allow_free_text"] is False


def test_input_required_payload_legacy_flat_form() -> None:
    """Legacy flat payload (`question`/`choices` top-level) 도 통과 (backward compat)."""
    payload = {
        "kind": "ask_user_question",
        "question": "Continue?",
        "choices": [
            {"id": "yes", "label": "Yes"},
            {"id": "no", "label": "No"},
        ],
        "allow_free_text": True,
    }
    result = _input_required_payload(payload, task_id="t-2")

    assert result["question"] == "Continue?"
    assert result["choices"] == [
        {"id": "yes", "label": "Yes"},
        {"id": "no", "label": "No"},
    ]
    assert result["allow_free_text"] is True


def test_input_required_payload_allow_other_propagates() -> None:
    """``allow_other=True`` 가 ``allow_free_text`` 로 변환."""
    payload = {
        "kind": "ask_user_question",
        "questions": [
            {"question": "?", "allow_other": True, "options": []},
        ],
    }
    result = _input_required_payload(payload, task_id="t-3")
    assert result["allow_free_text"] is True


def test_input_required_payload_empty_falls_back_to_default_question() -> None:
    """빈 payload 시 default 질문 텍스트 — modal 이 적어도 *뜨긴* 해야 한다."""
    result = _input_required_payload({}, task_id="t-4")
    assert result["question"] == "추가 결정이 필요합니다"
    assert result["choices"] == []
    assert result["allow_free_text"] is False


def test_input_required_payload_questions_missing_question_field() -> None:
    """``questions[0]`` 에 question 필드 누락 시 default 사용 (defensive)."""
    payload = {"kind": "ask_user_question", "questions": [{"options": []}]}
    result = _input_required_payload(payload, task_id="t-5")
    assert result["question"] == "추가 결정이 필요합니다"
    assert result["choices"] == []
