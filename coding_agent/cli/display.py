"""CLI 디스플레이 — Claude Code 스타일 출력."""

from __future__ import annotations

import sys
import time
import threading

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

# ── 트리 문자 ──
TREE_MID = "├─"
TREE_PIPE = "│"

# ── SubAgent 역할별 스타일 ──
ROLE_STYLES = {
    "planner":    "bold white on blue",
    "coder":      "bold white on green",
    "reviewer":   "bold white on dark_orange3",
    "fixer":      "bold white on red",
    "verifier":   "bold white on magenta",
    "researcher": "bold white on cyan",
}


# ── Live Spinner ──

class LiveSpinner:
    """SubAgent 도구 호출 시 회전 애니메이션을 표시하는 스피너.

    Features:
      - TTY 감지: 비-TTY 환경에서는 애니메이션 없이 1회만 출력
      - Frame throttling: 4 fps (250ms) — 스크롤백 노이즈 대폭 감소
      - Elapsed time: 5초 이상 같은 메시지면 경과 시간 자동 표시
    """

    CHARS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    FRAME_INTERVAL_S = 0.25  # 4 fps max (throttled)

    def __init__(self):
        self._message = ""
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._is_tty = sys.stdout.isatty()
        self._msg_start: float = 0.0  # 현재 메시지 시작 시각

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, message: str):
        with self._lock:
            self._message = message
            self._msg_start = time.monotonic()
            if self._running:
                return
            if not self._is_tty:
                # 비-TTY: 1회 출력 후 애니메이션 스레드 시작 안 함
                console.print(f"  {TREE_PIPE}  [dim]{message}[/dim]")
                return
            self._running = True
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()

    def update(self, message: str):
        with self._lock:
            if self._message == message:
                return  # 동일 메시지는 스킵 (중복 이벤트 방지)
            self._message = message
            self._msg_start = time.monotonic()
            if not self._is_tty:
                console.print(f"  {TREE_PIPE}  [dim]{message}[/dim]")

    def stop(self):
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=0.5)
            self._thread = None
        if self._is_tty:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

    def _animate(self):
        idx = 0
        last_msg: str | None = None
        last_write: float = 0.0
        while True:
            with self._lock:
                if not self._running:
                    break
                msg = self._message
                msg_start = self._msg_start
            now = time.monotonic()
            # 메시지 변경 시 즉시 출력, 아니면 FRAME_INTERVAL_S(0.25s) 간격으로 throttle
            if msg != last_msg or (now - last_write) >= self.FRAME_INTERVAL_S:
                char = self.CHARS[idx % len(self.CHARS)]
                elapsed = now - msg_start
                elapsed_str = f" [{elapsed:.0f}s]" if elapsed > 5 else ""
                sys.stdout.write(
                    f"\r  \033[2m{TREE_PIPE}\033[0m  {char} {msg}{elapsed_str}\033[K"
                )
                sys.stdout.flush()
                last_msg = msg
                last_write = now
                idx += 1
            time.sleep(0.05)  # 락 체크 주기만 짧게


_spinner = LiveSpinner()


def get_spinner() -> LiveSpinner:
    return _spinner


# ── SubAgent 표시 ──

def print_subagent_start(role: str, task: str) -> None:
    """SubAgent 위임 시작 — 트리 구조 + 역할 배지."""
    badge_style = ROLE_STYLES.get(role, "bold white on bright_black")
    task_truncated = task[:50] + "..." if len(task) > 50 else task
    console.print(
        f"  {TREE_MID} [{badge_style}] {role} [/{badge_style}]"
        f" [dim]{task_truncated}[/dim]"
    )


def print_subagent_done(
    elapsed: float, steps: int, tools: int, success: bool,
) -> None:
    """SubAgent 완료 — 결과 요약."""
    icon = ICON_OK if success else ICON_WARN
    style = "green" if success else "yellow"
    label = "completed" if success else "incomplete"
    console.print(
        f"  {TREE_PIPE}  {icon} [{style}]{label}[/{style}]"
        f" [dim]{elapsed:.1f}s · {steps} steps · {tools} tools[/dim]"
    )


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


# ── Sufficiency loop — critic_escalate panel ──

def render_critic_escalate_panel(event_data: dict) -> None:
    """Render an HITL ``critic_escalate`` notification as a red-bordered panel.

    Called from the CLI ``notifications`` consumer task whenever the
    sufficiency loop pushes a ``critic_escalate`` HITLEvent. Single-line
    ``[HITL]`` 출력 대신 panel 로 사용자 주의를 끈다.

    Expected ``event_data`` keys: ``reason`` (str), ``iteration`` (int),
    ``metrics`` (dict), ``answer_preview`` (str). 누락 키는 graceful 처리.
    """
    reason = event_data.get("reason") or "(reason 없음)"
    iteration = event_data.get("iteration", "?")
    metrics = event_data.get("metrics") or {}

    metric_lines: list[str] = []
    for k in (
        "pytest_exit", "lint_errors", "todo_done", "todo_total",
        "todo_ratio",
    ):
        if k in metrics:
            v = metrics[k]
            if isinstance(v, float):
                metric_lines.append(f"  [dim]·[/dim] {k}: {v:.2f}")
            else:
                metric_lines.append(f"  [dim]·[/dim] {k}: {v}")
    metrics_block = "\n".join(metric_lines) or "  [dim](신호 없음)[/dim]"

    body = (
        f"[bold]사유[/bold]\n{reason}\n\n"
        f"[bold]rule_gate metrics[/bold]\n{metrics_block}\n\n"
        f"[dim]iteration: {iteration} · 사용자 다시 질의 권장[/dim]"
    )

    console.print()
    console.print(
        Panel(
            body,
            title=f"[bold red]{ICON_WARN} 검토 필요 — sufficiency critic_escalate[/bold red]",
            border_style="red",
            padding=(1, 2),
        )
    )
    console.print()


def print_needs_human_review_footer(reason: str | None = None) -> None:
    """End-of-run footer when ``state.needs_human_review`` was True.

    The escalate event itself is shown live via :func:`render_critic_escalate_panel`;
    this footer is a final reminder so the user doesn't miss it after a long run.
    """
    txt = "[bold red]⚠ 이 응답은 사용자 검토가 권장됩니다.[/bold red]"
    if reason:
        txt += f"\n[dim]{reason}[/dim]"
    console.print(
        Panel(
            txt,
            border_style="red",
            padding=(0, 2),
        )
    )


# ── Todo ledger 표시 ──

_TODO_GLYPHS = {
    "pending": ("☐", "white"),
    "in_progress": ("◐", "yellow"),
    "completed": ("✓", "green"),
    # v22.4 — auto-verify chain 이 _AUTO_VERIFY_MAX_ATTEMPTS 회 fail 후
    # 자동 마킹하는 터미널 상태. 사용자 검토 필요 신호.
    "verify_failed": ("⚠", "red"),
}


def print_todo_panel(items: list) -> None:
    """Orchestrator todo ledger를 Rich Panel로 표시.

    write_todos / update_todo 호출 직후, 또는 task tool의 자동 마킹
    (B-1 auto_advance_todo) 직후 manager 콜백을 통해 호출된다.
    items는 ``TodoItem`` 인스턴스 리스트.

    Spinner-safe: task tool이 SubAgent 진행 중에 콜백을 발화시키면
    spinner의 carriage-return 라인과 패널 첫 줄이 같은 줄에 겹칠 수
    있어, 출력 전후로 spinner를 일시 정지·재개한다.
    """
    saved_msg: str | None = None
    was_running = _spinner.is_running
    if was_running:
        saved_msg = _spinner._message  # type: ignore[attr-defined]
        _spinner.stop()

    try:
        _render_todo_panel(items)
    finally:
        if was_running and saved_msg:
            _spinner.start(saved_msg)


def _render_todo_panel(items: list) -> None:
    if not items:
        console.print(
            Panel("[dim]todo ledger is empty[/dim]",
                  title="📋 Todos",
                  border_style="cyan",
                  padding=(0, 1))
        )
        return

    counts = {
        "pending": 0, "in_progress": 0, "completed": 0, "verify_failed": 0,
    }
    for it in items:
        status = getattr(it, "status", "pending")
        counts[status] = counts.get(status, 0) + 1

    lines: list[str] = []
    for it in items:
        status = getattr(it, "status", "pending")
        glyph, color = _TODO_GLYPHS.get(status, ("?", "white"))
        item_id = getattr(it, "id", "?")
        content = getattr(it, "content", "")
        if status == "completed":
            lines.append(f"[{color}]{glyph}[/{color}] [dim strike]{item_id}: {content}[/dim strike]")
        elif status == "in_progress":
            lines.append(f"[{color}]{glyph}[/{color}] [bold]{item_id}: {content}[/bold]")
        elif status == "verify_failed":
            # v22.4 — 자동 검증 한계 도달, 사용자 검토 필요. 빨강 + 굵게.
            lines.append(f"[{color}]{glyph}[/{color}] [bold red]{item_id}: {content}[/bold red]")
        else:
            lines.append(f"[{color}]{glyph}[/{color}] {item_id}: {content}")

    title_parts = [
        f"📋 Todos · {counts['completed']}/{len(items)} done",
        f"[yellow]{counts['in_progress']} active[/yellow]",
        f"[dim]{counts['pending']} pending[/dim]",
    ]
    if counts.get("verify_failed", 0) > 0:
        title_parts.append(f"[red]{counts['verify_failed']} verify-failed[/red]")
    title = " · ".join(title_parts)
    console.print(
        Panel(
            "\n".join(lines),
            title=title,
            border_style="cyan",
            padding=(0, 1),
        )
    )


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
| `/resume` | 중단된 작업 이어서 진행 |
| `/memory` | 저장된 메모리 목록 |
| `/memory add <layer> <key> <content>` | 메모리 수동 추가 |
| `/memory delete <key>` | 메모리 삭제 |
| `/agents` | SubAgent 인스턴스 목록 |
| `/events` | SubAgent 이벤트 로그 |
| `/status` | 시스템 상태 |
| `/exit` | 종료 |
"""
    console.print(Markdown(help_text))
