"""CLI renderer for ``ask_user_question`` interrupts.

Renders a Claude-Code-style multi-question prompt:

  ──────────────────────────────────────────────
   ☐ Tech stack  ☐ Mobile  ☐ Auth scope  ✔ Submit
  ──────────────────────────────────────────────

  Q1. Which tech stack should we use?
       1) FastAPI + React
          Python backend with React frontend.
       2) Node.js + Next.js
          Single-language full-stack.
       3) Type your own answer

  > 1

The implementation deliberately avoids interactive checkbox/list
widgets so it works in any TTY environment our CLI runs in (including
Docker pseudo-TTYs and non-prompt-toolkit hosts).
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

_SKIP_TOKEN = "(skipped)"


def _build_progress_bar(headers: list[str], answered: set[str]) -> Text:
    """Render the top progress bar like '☑ Tech stack ☐ Mobile ✔ Submit'."""
    parts: list[tuple[str, str]] = []
    for h in headers:
        mark = "☑" if h in answered else "☐"
        style = "bold green" if h in answered else "dim"
        parts.append((f"{mark} {h}  ", style))
    parts.append(("✔ Submit", "bold cyan"))

    text = Text()
    for chunk, style in parts:
        text.append(chunk, style=style)
    return text


def _ask_one(
    *,
    console: Console,
    index: int,
    total: int,
    question: dict[str, Any],
    headers: list[str],
    answered: set[str],
    input_fn,
) -> Any:
    """Render and prompt for a single question. Returns the answer.

    *input_fn* is the function used to read a line from the user; the
    test suite passes a stub. Returns:
      - str (free-form or single-select label)
      - list[str] (multi-select labels)
      - None when the user types '/skip'
    """
    options = question["options"]
    multi = question.get("multi_select", False)
    allow_other = question.get("allow_other", True)

    console.print()
    console.print(_build_progress_bar(headers, answered))
    console.print()

    title = f"[bold cyan]Q{index + 1}/{total}[/bold cyan]  [bold]{question['question']}[/bold]"
    body_lines: list[str] = []
    for i, opt in enumerate(options, start=1):
        body_lines.append(f"  [bold]{i}[/bold]) {opt['label']}")
        if opt.get("description"):
            body_lines.append(f"     [dim]{opt['description']}[/dim]")
    if allow_other:
        body_lines.append(f"  [bold]{len(options) + 1}[/bold]) [italic]Type your own answer[/italic]")
    body_lines.append("  [dim]/skip to skip this question[/dim]")
    if multi:
        body_lines.append("  [dim]Multi-select: comma-separated numbers (e.g. 1,3)[/dim]")

    console.print(Panel("\n".join([title, ""] + body_lines), border_style="cyan", expand=False))

    while True:
        raw = input_fn(f"  [Q{index + 1}] > ").strip()
        if raw == "/skip":
            return None
        if not raw:
            continue

        if multi:
            chosen: list[str] = []
            ok = True
            for token in raw.replace(" ", "").split(","):
                if not token.isdigit():
                    ok = False
                    break
                n = int(token)
                if n == len(options) + 1 and allow_other:
                    typed = input_fn("  free answer > ").strip()
                    if typed:
                        chosen.append(typed)
                elif 1 <= n <= len(options):
                    chosen.append(options[n - 1]["label"])
                else:
                    ok = False
                    break
            if not ok or not chosen:
                console.print("  [red]invalid input — try again[/red]")
                continue
            return chosen

        # Single select
        if raw.isdigit():
            n = int(raw)
            if n == len(options) + 1 and allow_other:
                typed = input_fn("  free answer > ").strip()
                return typed or None
            if 1 <= n <= len(options):
                return options[n - 1]["label"]
            console.print("  [red]invalid number — try again[/red]")
            continue

        # Anything else: treat as free-form
        return raw


def render_ask_user_question(
    payload: dict[str, Any],
    *,
    console: Console | None = None,
    input_fn=None,
) -> dict[str, Any]:
    """Render the bundled questions and collect answers.

    Returns a dict keyed by question header:
        {"Tech stack": "FastAPI + React", "Mobile": ["iOS", "Android"], ...}

    Headers whose answer is None are not included so the planner sees
    a clean dict and can detect skipped fields.
    """
    if console is None:
        console = Console()
    if input_fn is None:
        input_fn = input

    if not isinstance(payload, dict) or payload.get("kind") != "ask_user_question":
        raise ValueError(f"unexpected interrupt payload: {payload!r}")

    questions = payload.get("questions") or []
    headers = [q["header"] for q in questions]
    answered: set[str] = set()
    answers: dict[str, Any] = {}

    console.print()
    console.print("[bold cyan]◆ The agent needs your input[/bold cyan]")

    for i, q in enumerate(questions):
        ans = _ask_one(
            console=console,
            index=i,
            total=len(questions),
            question=q,
            headers=headers,
            answered=answered,
            input_fn=input_fn,
        )
        if ans is not None:
            answers[q["header"]] = ans
            answered.add(q["header"])

    console.print()
    console.print("[bold green]✔ Submitted[/bold green]")
    console.print()

    return answers
