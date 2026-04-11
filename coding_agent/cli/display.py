"""CLI 디스플레이 — Claude Code 스타일 출력."""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

# ── 아이콘 ──
ICON_TOOL = "⚡"
ICON_DELEGATE = "⇢"
ICON_OK = "✓"
ICON_WARN = "⚠"
ICON_ERROR = "✗"
ICON_MEMORY = "💾"
ICON_AGENT = "◆"
ICON_THINK = "●"


def print_welcome() -> None:
    console.print(
        Panel(
            f"[bold cyan]{ICON_AGENT} AX Coding Agent[/bold cyan]\n"
            "[dim]3-Layer Memory | Dynamic SubAgents | Resilient Loop[/dim]\n"
            "[dim]/help for commands · /exit to quit[/dim]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


def print_response(text: str) -> None:
    if not text.strip():
        return
    console.print()
    console.print(Markdown(text))
    console.print()


def print_tool_call(tool_name: str, brief: str = "") -> None:
    """도구 호출 실시간 표시 (Claude Code 스타일)."""
    truncated = brief[:80] + "..." if len(brief) > 80 else brief
    if truncated:
        console.print(f"  {ICON_TOOL} [cyan]{tool_name}[/cyan] [dim]{truncated}[/dim]")
    else:
        console.print(f"  {ICON_TOOL} [cyan]{tool_name}[/cyan]")


def print_tool_result(tool_name: str, result: str, is_error: bool = False) -> None:
    """도구 결과 표시."""
    if is_error:
        truncated = result[:120]
        console.print(f"    [red]↳ {truncated}[/red]")
    elif len(result) > 200:
        console.print(f"    [dim]↳ ({len(result)} chars)[/dim]")


def print_delegate(agent_type: str, task: str = "") -> None:
    """SubAgent 위임 표시."""
    truncated = task[:60] + "..." if len(task) > 60 else task
    console.print(f"  {ICON_DELEGATE} [yellow]위임: {agent_type}[/yellow] [dim]{truncated}[/dim]")


def print_agent_status(status: str, detail: str = "") -> None:
    """에이전트 상태 변경 표시."""
    console.print(f"  {ICON_OK} [green]{status}[/green] [dim]{detail}[/dim]")


def print_memory_event(action: str, key: str, layer: str) -> None:
    """메모리 이벤트 표시."""
    console.print(f"  {ICON_MEMORY} [magenta]{action}[/magenta] [{layer}] {key}")


def print_iteration_info(iteration: int, tier: str, model: str = "") -> None:
    """반복 정보 표시."""
    console.print(f"  [dim]iteration {iteration} · {tier}[/dim]")


def print_stall_warning(message: str) -> None:
    """StallDetector 경고 표시."""
    console.print(f"  {ICON_WARN} [yellow]{message}[/yellow]")


def print_status(message: str, style: str = "yellow") -> None:
    console.print(f"[{style}]{message}[/{style}]")


def print_error(message: str) -> None:
    console.print(f"  {ICON_ERROR} [bold red]{message}[/bold red]")


def print_memory_table(memories: list) -> None:
    table = Table(title="Stored Memories", show_lines=True)
    table.add_column("Layer", style="cyan", width=10)
    table.add_column("Category", style="green", width=15)
    table.add_column("Key", style="yellow", width=20)
    table.add_column("Content", width=50)
    for m in memories:
        table.add_row(m.layer, m.category, m.key, m.content[:80])
    console.print(table)


def print_agents_table(agents: list) -> None:
    table = Table(title="SubAgent Instances", show_lines=True)
    table.add_column("ID", style="cyan", width=12)
    table.add_column("Role", style="green", width=12)
    table.add_column("State", style="yellow", width=12)
    table.add_column("Task", width=40)
    table.add_column("Retries", width=8)
    for a in agents:
        state_style = {
            "running": "bold green", "completed": "green",
            "failed": "red", "blocked": "yellow", "destroyed": "dim",
        }.get(a.state.value, "white")
        table.add_row(
            a.agent_id, a.role,
            f"[{state_style}]{a.state.value}[/{state_style}]",
            a.task_summary[:60], str(a.retry_count),
        )
    console.print(table)


def print_event_log(events: list) -> None:
    table = Table(title="SubAgent Event Log", show_lines=True)
    table.add_column("Time", width=10)
    table.add_column("Agent", style="cyan", width=12)
    table.add_column("Transition", width=25)
    table.add_column("Reason", width=30)
    for e in events[-20:]:
        table.add_row(
            e.timestamp.strftime("%H:%M:%S"), e.agent_id,
            f"{e.from_state.value} → {e.to_state.value}", e.reason[:40],
        )
    console.print(table)


def print_help() -> None:
    help_text = """
| Command | Description |
|---------|-------------|
| `/help` | 도움말 |
| `/memory` | 저장된 메모리 목록 |
| `/memory add <layer> <key> <content>` | 메모리 수동 추가 |
| `/memory delete <key>` | 메모리 삭제 |
| `/agents` | SubAgent 인스턴스 목록 |
| `/events` | SubAgent 이벤트 로그 |
| `/status` | 시스템 상태 |
| `/exit` | 종료 |
"""
    console.print(Markdown(help_text))
