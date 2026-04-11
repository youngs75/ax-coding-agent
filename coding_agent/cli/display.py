"""CLI 디스플레이 유틸리티 — Rich 기반 출력 포매팅."""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()


def print_welcome() -> None:
    """시작 배너 출력."""
    console.print(
        Panel(
            "[bold cyan]AX Coding Agent[/bold cyan]\n"
            "[dim]3-Layer Memory | Dynamic SubAgents | Resilient Loop[/dim]\n"
            "[dim]Type /help for commands, /exit to quit[/dim]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


def print_response(text: str) -> None:
    """에이전트 응답을 Markdown으로 렌더링."""
    if not text.strip():
        return
    console.print()
    console.print(Markdown(text))
    console.print()


def print_status(message: str, style: str = "yellow") -> None:
    """상태 메시지 출력."""
    console.print(f"[{style}]{message}[/{style}]")


def print_error(message: str) -> None:
    """에러 메시지 출력."""
    console.print(f"[bold red]Error:[/bold red] {message}")


def print_memory_table(memories: list) -> None:
    """메모리 목록을 테이블로 출력."""
    table = Table(title="Stored Memories", show_lines=True)
    table.add_column("Layer", style="cyan", width=10)
    table.add_column("Category", style="green", width=15)
    table.add_column("Key", style="yellow", width=20)
    table.add_column("Content", width=50)

    for m in memories:
        table.add_row(m.layer, m.category, m.key, m.content[:80])

    console.print(table)


def print_agents_table(agents: list) -> None:
    """SubAgent 목록을 테이블로 출력."""
    table = Table(title="SubAgent Instances", show_lines=True)
    table.add_column("ID", style="cyan", width=12)
    table.add_column("Role", style="green", width=12)
    table.add_column("State", style="yellow", width=12)
    table.add_column("Task", width=40)
    table.add_column("Retries", width=8)

    for a in agents:
        state_style = {
            "running": "bold green",
            "completed": "green",
            "failed": "red",
            "blocked": "yellow",
            "destroyed": "dim",
        }.get(a.state.value, "white")

        table.add_row(
            a.agent_id,
            a.role,
            f"[{state_style}]{a.state.value}[/{state_style}]",
            a.task_summary[:60],
            str(a.retry_count),
        )

    console.print(table)


def print_event_log(events: list) -> None:
    """SubAgent 이벤트 로그 출력."""
    table = Table(title="SubAgent Event Log", show_lines=True)
    table.add_column("Time", width=20)
    table.add_column("Agent", style="cyan", width=12)
    table.add_column("Transition", width=25)
    table.add_column("Reason", width=30)

    for e in events[-20:]:  # 최근 20개
        table.add_row(
            e.timestamp.strftime("%H:%M:%S"),
            e.agent_id,
            f"{e.from_state.value} -> {e.to_state.value}",
            e.reason[:40],
        )

    console.print(table)


def print_help() -> None:
    """도움말 출력."""
    help_text = """
**Available Commands:**

| Command | Description |
|---------|-------------|
| `/help` | 이 도움말 표시 |
| `/memory` | 저장된 메모리 목록 표시 |
| `/memory add <layer> <key> <content>` | 메모리 수동 추가 |
| `/memory delete <key>` | 메모리 삭제 |
| `/agents` | SubAgent 인스턴스 목록 |
| `/events` | SubAgent 이벤트 로그 |
| `/status` | 현재 시스템 상태 |
| `/exit` | 종료 |
"""
    console.print(Markdown(help_text))
