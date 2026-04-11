"""대화형 CLI — Claude Code 스타일 스트리밍 출력.

사용법:
    python -m coding_agent.cli.app [workspace_path]
    ax-agent [workspace_path]
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory

from coding_agent.cli.display import (
    ICON_THINK,
    console,
    print_agents_table,
    print_agent_status,
    print_delegate,
    print_error,
    print_event_log,
    print_help,
    print_iteration_info,
    print_memory_event,
    print_memory_table,
    print_response,
    print_stall_warning,
    print_status,
    print_tool_call,
    print_tool_result,
    print_welcome,
)

# ── Lazy init ──
_loop = None


def _get_loop():
    global _loop
    if _loop is None:
        from coding_agent.core.loop import AgentLoop
        _loop = AgentLoop()
    return _loop


# ── 슬래시 커맨드 ──

def _handle_command(cmd: str) -> bool:
    parts = cmd.strip().split(maxsplit=3)
    command = parts[0].lower()
    loop = _get_loop()

    if command == "/help":
        print_help()
        return True

    elif command in ("/exit", "/quit"):
        print_status("Goodbye!", "cyan")
        loop.close()
        sys.exit(0)

    elif command == "/memory":
        store = loop.get_memory_store()
        if len(parts) >= 4 and parts[1] == "add":
            layer = parts[2]
            rest = parts[3].split(maxsplit=1)
            if len(rest) < 2:
                print_error("Usage: /memory add <layer> <key> <content>")
                return True
            key, content = rest[0], rest[1]
            from coding_agent.memory.schema import MemoryRecord
            store.upsert(MemoryRecord(layer=layer, category="manual", key=key, content=content))
            print_memory_event("stored", key, layer)
        elif len(parts) >= 3 and parts[1] == "delete":
            key = parts[2]
            for m in store.list_all():
                if m.key == key:
                    store.delete(m.id)
                    print_agent_status(f"deleted: {key}")
            return True
        else:
            memories = store.list_all()
            if memories:
                print_memory_table(memories)
            else:
                print_status("No memories stored yet.", "dim")
        return True

    elif command == "/agents":
        registry = loop.get_registry()
        agents = registry.get_active()
        if agents:
            print_agents_table(agents)
        else:
            print_status("No active SubAgents.", "dim")
        return True

    elif command == "/events":
        registry = loop.get_registry()
        events = registry.event_log
        if events:
            print_event_log(events)
        else:
            print_status("No events yet.", "dim")
        return True

    elif command == "/status":
        from coding_agent.config import get_config
        cfg = get_config()
        tier = cfg.model_tier
        console.print(f"  [cyan]Provider:[/cyan] {cfg.provider}")
        console.print(f"  [cyan]REASONING:[/cyan] {tier.reasoning}")
        console.print(f"  [cyan]STRONG:[/cyan]    {tier.strong}")
        console.print(f"  [cyan]DEFAULT:[/cyan]   {tier.default}")
        console.print(f"  [cyan]FAST:[/cyan]      {tier.fast}")
        console.print(f"  [cyan]Memory DB:[/cyan] {cfg.memory_db_path}")
        console.print(f"  [cyan]Timeout:[/cyan]   {cfg.llm_timeout}s")
        return True

    return False


# ── 스트리밍 에이전트 실행 ──

async def _run_agent_streaming(user_input: str) -> None:
    """LangGraph astream_events로 실시간 도구 호출/메모리 이벤트를 표시."""
    loop = _get_loop()
    graph = loop._graph
    store = loop.get_memory_store()

    from langchain_core.messages import HumanMessage
    from coding_agent.config import get_config

    initial_state = {
        "messages": [HumanMessage(content=user_input)],
        "project_id": "",
        "working_directory": os.getcwd(),
    }

    loop._progress_guard.reset()
    start_time = time.time()
    final_content = ""
    iteration = 0
    shown_tools = set()

    console.print(f"  [dim]{ICON_THINK} thinking...[/dim]", end="\r")

    try:
        async for event in graph.astream_events(initial_state, version="v2"):
            kind = event.get("event", "")
            name = event.get("name", "")
            data = event.get("data", {})

            # ── 도구 호출 시작 ──
            if kind == "on_tool_start":
                tool_input = data.get("input", {})
                brief = ""
                if isinstance(tool_input, dict):
                    # 파일 경로나 명령어를 간략히 표시
                    brief = tool_input.get("path", "")
                    if not brief:
                        brief = tool_input.get("command", "")
                    if not brief:
                        brief = tool_input.get("pattern", "")
                    if not brief:
                        brief = tool_input.get("description", "")[:60]
                print_tool_call(name, brief)

            # ── 도구 호출 완료 ──
            elif kind == "on_tool_end":
                output = data.get("output", "")
                output_str = str(output.content) if hasattr(output, "content") else str(output)
                is_error = "error" in output_str.lower()[:50]
                if is_error or len(output_str) > 200:
                    print_tool_result(name, output_str, is_error=is_error)

            # ── LLM 호출 시작 ──
            elif kind == "on_chat_model_start":
                iteration += 1
                console.print(f"\r  [dim]{ICON_THINK} thinking... (step {iteration})[/dim]", end="\r")

            # ── LLM 스트리밍 토큰 ──
            elif kind == "on_chat_model_stream":
                chunk = data.get("chunk", None)
                if chunk and hasattr(chunk, "content") and chunk.content:
                    content = chunk.content
                    if isinstance(content, str):
                        final_content += content

            # ── 노드 완료 ──
            elif kind == "on_chain_end" and name in (
                "extract_memory", "extract_memory_final"
            ):
                # 메모리 추출 완료 시
                pass

            # ── SubAgent 위임 감지 ──
            elif kind == "on_tool_start" and name == "task":
                tool_input = data.get("input", {})
                desc = tool_input.get("description", "")[:60] if isinstance(tool_input, dict) else ""
                agent_type = tool_input.get("agent_type", "auto") if isinstance(tool_input, dict) else "auto"
                print_delegate(agent_type, desc)

    except KeyboardInterrupt:
        print_status("\n  Interrupted.", "yellow")
        return
    except Exception as e:
        print_error(str(e))
        return

    elapsed = time.time() - start_time
    console.print(f"\r{'':80}")  # 클리어

    # ── 최종 응답 출력 ──
    if final_content.strip():
        print_response(final_content)
    else:
        # 스트리밍 못 받았으면 마지막 메시지에서 추출
        try:
            state = await graph.aget_state(graph.checkpointer) if hasattr(graph, "checkpointer") else None
        except Exception:
            state = None

    # ── 메모리 저장 이벤트 표시 ──
    memories = store.list_all()
    if memories:
        recent = [m for m in memories if m.updated_at and m.updated_at > ""]
        # 최근 저장된 메모리 수만 표시
        new_count = min(len(recent), 5)
        if new_count > 0:
            print_memory_event("extracted", f"{new_count} memories", "auto")

    # ── 완료 정보 ──
    print_agent_status("completed", f"{elapsed:.1f}s · {iteration} steps")


# ── 폴백: 비스트리밍 실행 ──

async def _run_agent_simple(user_input: str) -> None:
    """스트리밍 안 될 때 폴백."""
    loop = _get_loop()

    with console.status(f"[bold cyan]{ICON_THINK} thinking...", spinner="dots"):
        result = await loop.run(user_input)

    exit_reason = result.get("exit_reason", "")
    if exit_reason and exit_reason not in ("completed", ""):
        style_map = {
            "safe_stop": "yellow", "progress_guard_stop": "yellow",
            "error_abort": "red", "all_models_exhausted": "red",
        }
        print_stall_warning(exit_reason)

    response = result.get("final_response", "")
    print_response(response)

    iterations = result.get("iteration", 0)
    if iterations > 0:
        print_agent_status("completed", f"{iterations} steps")


# ── 메인 루프 ──

async def _async_main() -> None:
    print_welcome()

    # 작업 디렉토리 표시
    cwd = os.getcwd()
    console.print(f"  [dim]workspace: {cwd}[/dim]")
    console.print()

    history_path = os.path.expanduser("~/.ax_agent_history")
    session: PromptSession = PromptSession(
        history=FileHistory(history_path),
        auto_suggest=AutoSuggestFromHistory(),
    )

    while True:
        try:
            user_input = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: session.prompt("\n[You] > "),
            )
        except (EOFError, KeyboardInterrupt):
            print_status("\nGoodbye!", "cyan")
            _get_loop().close()
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        if user_input.startswith("/"):
            if _handle_command(user_input):
                continue

        try:
            await _run_agent_streaming(user_input)
        except Exception:
            # 스트리밍 실패 시 폴백
            try:
                await _run_agent_simple(user_input)
            except Exception as e:
                print_error(str(e))


def main() -> None:
    """CLI 엔트리포인트."""
    # 작업 디렉토리 인자
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        work_dir = os.path.abspath(sys.argv[1])
        if not os.path.isdir(work_dir):
            os.makedirs(work_dir, exist_ok=True)
        os.chdir(work_dir)

    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
