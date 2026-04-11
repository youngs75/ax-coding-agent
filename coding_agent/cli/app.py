"""대화형 CLI — Rich + prompt-toolkit 기반 REPL.

사용법:
    python -m coding_agent.cli.app
    또는
    ax-agent
"""

from __future__ import annotations

import asyncio
import sys
import os

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner

from coding_agent.cli.display import (
    console,
    print_agents_table,
    print_error,
    print_event_log,
    print_help,
    print_memory_table,
    print_response,
    print_status,
    print_welcome,
)

# ── Lazy init to avoid import-time side effects ──
_loop: "AgentLoop | None" = None


def _get_loop():
    global _loop
    if _loop is None:
        from coding_agent.core.loop import AgentLoop
        _loop = AgentLoop()
    return _loop


def _handle_command(cmd: str) -> bool:
    """슬래시 커맨드를 처리한다. 처리했으면 True 반환."""
    parts = cmd.strip().split(maxsplit=3)
    command = parts[0].lower()
    loop = _get_loop()

    if command == "/help":
        print_help()
        return True

    elif command == "/exit" or command == "/quit":
        print_status("Goodbye!", "cyan")
        loop.close()
        sys.exit(0)

    elif command == "/memory":
        store = loop.get_memory_store()
        if len(parts) >= 4 and parts[1] == "add":
            # /memory add <layer> <key> <content>
            layer = parts[2]
            rest = parts[3].split(maxsplit=1)
            if len(rest) < 2:
                print_error("Usage: /memory add <layer> <key> <content>")
                return True
            key, content = rest[0], rest[1]
            from coding_agent.memory.schema import MemoryRecord
            record = MemoryRecord(layer=layer, category="manual", key=key, content=content)
            store.upsert(record)
            print_status(f"Memory added: [{layer}] {key}", "green")
        elif len(parts) >= 3 and parts[1] == "delete":
            # /memory delete <key>
            key = parts[2]
            all_memories = store.list_all()
            deleted = False
            for m in all_memories:
                if m.key == key:
                    store.delete(m.id)
                    deleted = True
            if deleted:
                print_status(f"Memory deleted: {key}", "green")
            else:
                print_error(f"Memory not found: {key}")
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
            print_status("No SubAgent events yet.", "dim")
        return True

    elif command == "/status":
        from coding_agent.config import get_config
        cfg = get_config()
        tier = cfg.model_tier
        console.print(f"[cyan]Provider:[/cyan] {cfg.provider}")
        console.print(f"[cyan]Models:[/cyan]")
        console.print(f"  REASONING: {tier.reasoning}")
        console.print(f"  STRONG:    {tier.strong}")
        console.print(f"  DEFAULT:   {tier.default}")
        console.print(f"  FAST:      {tier.fast}")
        console.print(f"[cyan]Memory DB:[/cyan] {cfg.memory_db_path}")
        console.print(f"[cyan]Max Iterations:[/cyan] {cfg.max_iterations}")
        console.print(f"[cyan]LLM Timeout:[/cyan] {cfg.llm_timeout}s")
        return True

    return False


async def _run_agent(user_input: str) -> None:
    """에이전트를 실행하고 결과를 출력한다."""
    loop = _get_loop()

    with console.status("[bold cyan]Thinking...", spinner="dots"):
        result = await loop.run(user_input)

    # 종료 사유 표시
    exit_reason = result.get("exit_reason", "")
    if exit_reason and exit_reason not in ("completed", ""):
        style_map = {
            "safe_stop": "yellow",
            "progress_guard_stop": "yellow",
            "error_abort": "red",
            "all_models_exhausted": "red",
            "max_iterations": "yellow",
        }
        style = style_map.get(exit_reason, "yellow")
        print_status(f"[{exit_reason}]", style)

    # 응답 출력
    response = result.get("final_response", "")
    print_response(response)

    # 이터레이션 수 표시
    iterations = result.get("iteration", 0)
    if iterations > 1:
        print_status(f"({iterations} iterations)", "dim")


async def _async_main() -> None:
    """비동기 메인 루프."""
    print_welcome()

    # 히스토리 파일
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

        # 슬래시 커맨드 처리
        if user_input.startswith("/"):
            if _handle_command(user_input):
                continue

        # 에이전트 실행
        try:
            await _run_agent(user_input)
        except KeyboardInterrupt:
            print_status("\nInterrupted.", "yellow")
        except Exception as e:
            print_error(str(e))


def main() -> None:
    """CLI 엔트리포인트.

    사용법:
        ax-agent                     # 현재 디렉토리에서 실행
        ax-agent /path/to/project    # 지정된 디렉토리에서 실행
    """
    import os

    # 작업 디렉토리 인자 처리
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        work_dir = os.path.abspath(sys.argv[1])
        if not os.path.isdir(work_dir):
            os.makedirs(work_dir, exist_ok=True)
            print_status(f"Created directory: {work_dir}", "green")
        os.chdir(work_dir)
        print_status(f"Working directory: {work_dir}", "cyan")

    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
