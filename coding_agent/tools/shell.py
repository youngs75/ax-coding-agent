"""Shell command execution tool — hardened against hangs.

Hardening rules
---------------
1. Per-command timeout is enforced by the harness, not the LLM. The LLM
   never sees a ``timeout`` parameter, so it cannot push it to 600s+
   when commands hang.
2. ``stdin`` is always closed (``subprocess.DEVNULL``) so interactive
   prompts (npm create, apt-get) hit EOF instead of blocking forever.
3. Known interactive commands are auto-corrected (``--yes`` / ``-y``)
   with a warning prefix so the LLM learns the pattern from the output.
4. Timeout errors are returned in English with a clear diagnosis and
   suggested next action so the LLM can pick a different approach.

These rules together kill the "LLM retries the same hung command" loop
that the previous E2E hit on ``npm create vite@latest .``.
"""

from __future__ import annotations

import os
import re
import subprocess

from langchain_core.tools import tool
from pydantic import BaseModel, Field

# ── Configuration ───────────────────────────────────────────────────

# Hard upper bound. The harness — not the LLM — owns this number.
_EXECUTE_TIMEOUT_DEFAULT = 300
_EXECUTE_TIMEOUT_MAX = 600


def _resolve_timeout() -> int:
    """Resolve the per-command timeout from env, clamped to [30, MAX]."""
    raw = os.environ.get("EXECUTE_TIMEOUT")
    if not raw:
        return _EXECUTE_TIMEOUT_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return _EXECUTE_TIMEOUT_DEFAULT
    return max(30, min(value, _EXECUTE_TIMEOUT_MAX))


# ── Dangerous-command guard ─────────────────────────────────────────

# Each entry is a regex compiled with re.IGNORECASE. Substring matching
# is too loose: it would block `rm -rf /workspace/build` because of the
# `rm -rf /` substring. The patterns below target the *actually* fatal
# shapes only.
_DANGEROUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    # rm -rf / (root), rm -rf /*, rm -rf / something
    re.compile(r"\brm\s+-rf?\s+/(?:\s|$|\*)", re.IGNORECASE),
    # rm -rf on system roots
    re.compile(
        r"\brm\s+-rf?\s+/(bin|sbin|usr|etc|var|boot|lib|dev|proc|sys|root)\b",
        re.IGNORECASE,
    ),
    # mkfs.* (filesystem format)
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    # dd if= writing to a block device
    re.compile(r"\bdd\s+if=", re.IGNORECASE),
    # classic fork bomb
    re.compile(r":\(\)\s*\{"),
)


def _is_dangerous(command: str) -> bool:
    return any(p.search(command) for p in _DANGEROUS_PATTERNS)


# ── Interactive-command auto-correction ─────────────────────────────
#
# Each entry is (pattern, replacement, human-readable reason).
# The pattern matches the command (or a substring) that is known to
# block on stdin without an explicit "say yes" flag. The replacement
# is what we substitute in. We deliberately keep the rules narrow —
# anything broader risks corrupting working commands.

_AUTO_FIX_RULES: list[tuple[re.Pattern[str], str, str]] = [
    # apt-get / apt install ...  →  apt-get install -y ...
    (
        re.compile(r"\b(apt(?:-get)?)\s+install\b(?!\s+(?:-y|--yes))"),
        r"\1 install -y",
        "apt install needs -y to run non-interactively",
    ),
    # apt-get update is non-interactive but apt-get upgrade isn't.
    (
        re.compile(r"\b(apt(?:-get)?)\s+upgrade\b(?!\s+(?:-y|--yes))"),
        r"\1 upgrade -y",
        "apt upgrade needs -y to run non-interactively",
    ),
    # npm create <pkg> [args]   (no --yes anywhere)
    (
        re.compile(r"\bnpm\s+create\s+(\S+)(?!.*--yes)"),
        r"npm create \1 --yes",
        "npm create prompts on stdin without --yes",
    ),
    # npm init <pkg>            (no --yes)
    (
        re.compile(r"\bnpm\s+init\s+(\S+)(?!.*--yes)"),
        r"npm init \1 --yes",
        "npm init prompts on stdin without --yes",
    ),
    # bare `npm init`           (no --yes)
    (
        re.compile(r"\bnpm\s+init\s*(?:&&|;|\|\||$)(?!.*--yes)"),
        "npm init --yes",
        "npm init prompts on stdin without --yes",
    ),
    # npx create-<something>    (no --yes)
    (
        re.compile(r"\bnpx\s+(create-\S+)(?!.*--yes)"),
        r"npx \1 --yes",
        "npx create-* prompts on stdin without --yes",
    ),
]


def _autofix_command(command: str) -> tuple[str, list[str]]:
    """Apply known auto-fixes. Returns (fixed_command, list_of_reasons)."""
    fixed = command
    reasons: list[str] = []
    for pattern, replacement, reason in _AUTO_FIX_RULES:
        new = pattern.sub(replacement, fixed)
        if new != fixed:
            fixed = new
            reasons.append(reason)
    return fixed, reasons


# ── Execute tool ────────────────────────────────────────────────────


class ExecuteInput(BaseModel):
    """Input schema for the ``execute`` tool.

    Note: ``timeout`` is intentionally NOT exposed. The harness owns it
    via the EXECUTE_TIMEOUT environment variable, capped to 600 seconds.
    """

    command: str = Field(description="The shell command to run")
    working_directory: str = Field(default=".", description="Working directory")


@tool("execute", args_schema=ExecuteInput)
def execute(command: str, working_directory: str = ".") -> str:
    """Run a shell command with stdin closed and a hard timeout.

    The command runs with /dev/null as stdin so interactive prompts
    cannot block. A timeout (default 300s, capped at 600s) is enforced
    by the harness; the LLM cannot extend it.
    """
    if _is_dangerous(command):
        return f"Error: dangerous command blocked: {command}"

    timeout = _resolve_timeout()

    fixed_command, fix_reasons = _autofix_command(command)
    notice = ""
    if fix_reasons:
        notice = (
            "[notice] command auto-corrected for non-interactive execution: "
            + "; ".join(fix_reasons)
            + f"\n[notice] running: {fixed_command}\n"
        )

    try:
        result = subprocess.run(
            fixed_command,
            shell=True,
            cwd=working_directory,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"

        max_chars = 10000
        if len(output) > max_chars:
            output = output[:max_chars] + f"\n... (truncated, {len(output)} total chars)"

        return (notice + output.strip()) or "(no output)"

    except subprocess.TimeoutExpired:
        return (
            notice
            + f"[TIMEOUT] Command exceeded {timeout}s and was terminated.\n"
            + "Likely causes: (a) waiting on stdin (interactive prompt), "
            + "(b) network call hanging, (c) build step in an infinite loop, "
            + "(d) blocking dev server (npm run dev / start). "
            + "Do NOT retry the same command — pick a different approach: "
            + "use --yes/-y flags, run servers in background with '&', "
            + "or break the work into smaller commands.\n"
            + f"Command was: {fixed_command}"
        )
    except Exception as e:
        return notice + f"Error: {e}"


SHELL_TOOLS = [execute]
