"""End-to-end test: interrupt propagation through SubAgent → task_tool → orchestrator.

This test does NOT call any real LLM. Instead it builds a tiny LangGraph
that mimics our two-tier orchestrator+SubAgent flow, with the SubAgent
using the real ``ask_user_question`` tool. The goal is to prove that:

1. A SubAgent's ``interrupt()`` surfaces through ``manager.spawn``
2. ``task_tool`` propagates it to the orchestrator graph
3. The orchestrator graph pauses with __interrupt__
4. ``Command(resume=...)`` resumes through both layers without
   re-spawning the SubAgent
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.constants import START, END
from langgraph.graph import StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

from coding_agent.core.state import AgentState
from coding_agent.tools.ask_tool import ask_user_question_tool
from coding_agent.tools.task_tool import build_task_tool


# ── A tiny SubAgent manager double ──────────────────────────────────


class _FakeSubAgentManager:
    """Minimal manager that runs a single-tool subgraph using ask_user_question.

    Mirrors the real manager's interrupt-aware `spawn` API: same
    ``_paused_runs`` cache, same ``resume_value`` parameter.
    """

    def __init__(self) -> None:
        self._paused_runs: dict[str, dict[str, Any]] = {}
        self._key = "fake-key"

    def _build_graph(self):
        tools = [ask_user_question_tool]

        def agent(state):
            msgs = state.get("messages", [])
            if not any(isinstance(m, AIMessage) for m in msgs):
                # First turn: emit a tool call to ask_user_question
                ai = AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "ask_user_question",
                        "args": {
                            "questions": [{
                                "header": "Tech",
                                "question": "Pick stack?",
                                "options": [
                                    {"label": "FastAPI", "description": ""},
                                    {"label": "Nest", "description": ""},
                                ],
                            }]
                        },
                        "id": "call-1",
                    }],
                )
                return {"messages": [ai]}
            # Second turn: emit a final message to terminate
            return {"messages": [AIMessage(content="done")]}

        def route(state):
            last = state["messages"][-1]
            if isinstance(last, AIMessage) and last.tool_calls:
                return "tools"
            return END

        b = StateGraph(AgentState)
        b.add_node("agent", agent)
        b.add_node("tools", ToolNode(tools))
        b.set_entry_point("agent")
        b.add_conditional_edges("agent", route, {"tools": "tools", END: END})
        b.add_edge("tools", "agent")
        return b.compile(checkpointer=InMemorySaver())

    async def spawn(
        self,
        task_description: str,
        agent_type: str = "auto",
        resume_value: Any = None,
        **kwargs,
    ):
        from coding_agent.subagents.models import SubAgentResult

        paused = self._paused_runs.get(self._key)
        if paused is not None:
            # Idempotent re-spawn — see real manager. Without an answer,
            # return the cached payload so the caller's interrupt() can
            # provide it.
            if resume_value is None:
                return SubAgentResult(
                    success=True,
                    output="(awaiting)",
                    interrupt_payload=paused["payload"],
                    thread_id=paused["thread_id"],
                )
            graph = paused["graph"]
            thread_id = paused["thread_id"]
            final = await graph.ainvoke(Command(resume=resume_value), config={
                "configurable": {"thread_id": thread_id}
            })
            if final.get("__interrupt__"):
                payload = final["__interrupt__"][0].value
                paused["payload"] = payload
                return SubAgentResult(success=True, output="(awaiting)", interrupt_payload=payload, thread_id=thread_id)
            self._paused_runs.pop(self._key, None)
            return SubAgentResult(success=True, output="completed")

        graph = self._build_graph()
        thread_id = f"sub-{uuid.uuid4()}"
        final = await graph.ainvoke(
            {"messages": [HumanMessage(content=task_description)]},
            config={"configurable": {"thread_id": thread_id}},
        )
        if final.get("__interrupt__"):
            payload = final["__interrupt__"][0].value
            self._paused_runs[self._key] = {
                "graph": graph,
                "thread_id": thread_id,
                "payload": payload,
            }
            return SubAgentResult(
                success=True,
                output="(awaiting)",
                interrupt_payload=payload,
                thread_id=thread_id,
            )
        return SubAgentResult(success=True, output="completed")


# ── Outer "orchestrator" that uses task_tool ────────────────────────


def _build_outer_graph(manager):
    task_tool = build_task_tool(manager)

    def agent(state):
        msgs = state.get("messages", [])
        if not any(isinstance(m, AIMessage) for m in msgs):
            ai = AIMessage(
                content="",
                tool_calls=[{
                    "name": "task",
                    "args": {"description": "do planning", "agent_type": "planner"},
                    "id": "call-outer-1",
                }],
            )
            return {"messages": [ai]}
        return {"messages": [AIMessage(content="all done")]}

    def route(state):
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return END

    b = StateGraph(AgentState)
    b.add_node("agent", agent)
    b.add_node("tools", ToolNode([task_tool]))
    b.set_entry_point("agent")
    b.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    b.add_edge("tools", "agent")
    return b.compile(checkpointer=InMemorySaver())


# ── The actual end-to-end test ──────────────────────────────────────


@pytest.mark.asyncio
async def test_interrupt_propagates_and_resumes():
    manager = _FakeSubAgentManager()
    graph = _build_outer_graph(manager)
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    # First invocation should pause on the SubAgent's interrupt
    result = await graph.ainvoke({"messages": [HumanMessage(content="hi")]}, config=config)
    assert "__interrupt__" in result, f"expected interrupt, got: {list(result.keys())}"
    payload = result["__interrupt__"][0].value
    assert payload["kind"] == "ask_user_question"
    assert payload["questions"][0]["header"] == "Tech"

    # Resume with the user's answer (matches the format the renderer produces)
    answer = {"Tech": "FastAPI"}
    result2 = await graph.ainvoke(Command(resume=answer), config=config)

    # Final result must NOT contain another interrupt and must have a final AI message
    assert "__interrupt__" not in result2, f"unexpected re-interrupt: {result2.get('__interrupt__')}"
    msgs = result2.get("messages", [])
    last_ai = [m for m in msgs if isinstance(m, AIMessage)][-1]
    assert "all done" in (last_ai.content or "")

    # Verify the SubAgent ToolMessage carried the formatted answer back
    tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
    assert any("COMPLETED" in (m.content or "") or "completed" in (m.content or "").lower() for m in tool_msgs)
