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
5. CI-style environment variables are injected by default so test
   runners (vitest, jest, vite, next) do not enter watch mode.
6. Known watch/daemon commands (vitest with no 'run', npm run dev,
   etc.) are rejected at the boundary with a concrete alternative,
   because CI=1 does not cover every tool consistently.

These rules together kill the "LLM retries the same hung command" loop
that the previous E2E hit on ``npm create vite@latest .`` and on
``vitest`` (default watch mode).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess

from langchain_core.tools import tool
from pydantic import BaseModel, Field

# ── Configuration ───────────────────────────────────────────────────

# Hard upper bound. The harness — not the LLM — owns this number.
#
# Timeout history:
#   v1   300s — original default. v8 E2E showed pytest collection
#                hang on a broken conftest.py burned a full 5-minute
#                window before the LLM noticed.
#   v8.1  90s — single biggest sink in 449s verifier hang was the
#                first execute call running without an inline timeout
#                prefix and consuming the full default. 90s comfortably
#                covers normal pytest/build/install runs (median <30s
#                in v8 traces) while killing collection hangs early.
#                Users with legitimately longer steps can raise it via
#                the EXECUTE_TIMEOUT env var, capped at MAX below.
_EXECUTE_TIMEOUT_DEFAULT = 90
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


# ── Watch/daemon command guard ──────────────────────────────────────
#
# Even with CI=1 injected (see _build_env), some commands are designed
# to never exit: dev servers (`npm run dev`, `vite`, `next dev`) and
# explicit watch flags (`--watch`, `-w`, `--serve`).  Letting the
# subagent call these would either hit the 300s timeout (wasting the
# budget) or pass ``CI=1`` to a tool that ignores it.  We reject at the
# tool boundary with a specific alternative, so the LLM switches
# approach immediately instead of burning the whole timeout.
#
# Key patterns, each with a short reason so the REJECTED message is
# actionable.  All regex are case-insensitive and anchored on word
# boundaries to avoid false positives like `test-server-dev-tools`.

_WATCH_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Explicit watch flags on any command.
    (
        re.compile(r"(?:^|[\s;&|])(?:--watch|--watchAll(?!=false)|--watch-all)\b", re.IGNORECASE),
        "the --watch flag runs forever. Use the one-shot form instead "
        "(vitest → 'vitest run', jest → 'jest --watchAll=false').",
    ),
    (
        re.compile(r"(?:^|[\s;&|])-w\b(?!\s*=\s*false)", re.IGNORECASE),
        "-w enables watch mode. Run the test command once and exit instead.",
    ),
    # `vitest` with no subcommand is watch by default.
    (
        re.compile(r"(?:^|[\s;&|])(?:npx\s+)?vitest(?!\s+(?:run|--run|related|list|bench))", re.IGNORECASE),
        "bare 'vitest' enters watch mode by default. Use 'vitest run' "
        "to execute tests once and exit.",
    ),
    # Dev / serve scripts on common JS package managers.
    (
        re.compile(r"(?:^|[\s;&|])(?:npm|yarn|pnpm)\s+(?:run\s+)?(?:dev|start|serve|watch)\b", re.IGNORECASE),
        "dev/start/serve/watch scripts are long-running servers. "
        "The harness cannot collect their output. If you need to "
        "smoke-test a server, run 'npm run build' then inspect the "
        "output artifacts instead.",
    ),
    # Direct CLI invocations of dev servers.
    (
        re.compile(r"(?:^|[\s;&|])(?:npx\s+)?(?:vite|next|nuxt|remix|parcel)\s+(?:dev|serve|start)\b", re.IGNORECASE),
        "this is a dev server that never exits. Use the build command "
        "('vite build', 'next build', etc.) if you need to verify the project.",
    ),
    (
        re.compile(r"(?:^|[\s;&|])(?:npx\s+)?(?:vite|next|nuxt)(?:\s*$|\s+[-;&|])", re.IGNORECASE),
        "bare 'vite' / 'next' / 'nuxt' launches a dev server. Use the "
        "explicit build subcommand instead ('vite build', 'next build').",
    ),
    # webpack-dev-server is always a dev server, bare or with args.
    (
        re.compile(r"(?:^|[\s;&|])(?:npx\s+)?webpack-dev-server\b", re.IGNORECASE),
        "webpack-dev-server runs forever. Use 'webpack' (or 'webpack build') "
        "to produce a one-shot bundle instead.",
    ),
    # Python / generic dev servers.
    (
        re.compile(r"(?:^|[\s;&|])(?:python\s+-m\s+)?http\.server\b", re.IGNORECASE),
        "http.server runs forever. Use curl against a production server "
        "or inspect files directly with read_file.",
    ),
    (
        re.compile(r"(?:^|[\s;&|])(?:flask\s+run|uvicorn|gunicorn|hypercorn|waitress-serve)\b", re.IGNORECASE),
        "dev/app servers never exit from the harness's perspective. "
        "Use the CLI's build or test entrypoint instead.",
    ),
    # File watchers.
    (
        re.compile(r"(?:^|[\s;&|])(?:tsc\s+(?:--watch|-w)|nodemon|pm2\s+(?:start|restart))\b", re.IGNORECASE),
        "this command watches files forever. Use the one-shot form "
        "('tsc' without --watch) or skip it.",
    ),
    # v22.2 — direct node server invocation (server.js / app.js / main.js / index.js).
    # 'npm run dev' 패턴이 잡히지만 coder 가 우회해서 'node server.js' 직접
    # 호출하면 통과 → infinite hang (v24 회귀, 2026-04-26).
    (
        re.compile(
            r"(?:^|[\s;&|])(?:node(?:js)?|deno\s+run|bun\s+run)\s+(?:\S+/)?"
            r"(?:server|app|main|index|start|bootstrap)\.(?:js|mjs|cjs|ts|tsx)\b",
            re.IGNORECASE,
        ),
        "direct node/deno/bun 서버 스크립트 호출은 daemon 으로 영원히 실행됩니다. "
        "테스트로 검증하려면 jest/vitest 의 supertest 패턴을 사용하세요. "
        "수동 smoke test 가 필요하면 build 산출물을 별도 컨테이너에서 실행하세요.",
    ),
    # v22.2 — trailing '&' (shell backgrounding). subprocess 가 backgrounded
    # process 를 reap 하지 못해 execute 가 block. 또한 background process 는
    # execute return 후 곧 SIGHUP 으로 죽거나 좀비로 남음.
    (
        re.compile(r"&\s*$"),
        "trailing '&' (백그라운드 실행) 은 execute 도구로는 의미 없습니다 — "
        "subprocess 가 reap 안 되거나 return 후 죽습니다. 진짜로 daemon 이 "
        "필요하면 docker-compose 또는 build 산출물을 별도 컨테이너에서 "
        "실행하세요. 단순 검증이면 supertest 류 in-process 테스트로 대체.",
    ),
)


def _is_watch_command(command: str) -> tuple[bool, str]:
    """Return (True, reason) if *command* would run forever."""
    for pattern, reason in _WATCH_PATTERNS:
        if pattern.search(command):
            return True, reason
    return False, ""


# ── Environment defaults ────────────────────────────────────────────
#
# These match the environment variables that CI systems (GitHub
# Actions, CircleCI, etc.) set automatically.  Most modern dev tools
# respect at least one of them to disable watch modes, color codes,
# and interactive prompts.  Injecting them by default makes the
# SubAgent environment behave like a CI runner — which is conceptually
# exactly what it is.

_CI_ENV_DEFAULTS: dict[str, str] = {
    "CI": "1",                           # vitest/jest/vite/next: no watch
    "DEBIAN_FRONTEND": "noninteractive",  # apt-get: no tzdata prompt
    "NO_COLOR": "1",                     # cleaner logs
    "TERM": "dumb",                      # prevents tools from probing TTY
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",                 # pip: never ask
    "PYTHONUNBUFFERED": "1",             # flush python stdout promptly
    "NPM_CONFIG_COLOR": "false",
    "NPM_CONFIG_PROGRESS": "false",
    "NPM_CONFIG_FUND": "false",
    "NPM_CONFIG_AUDIT": "false",
    "FORCE_COLOR": "0",
}


def _build_env() -> dict[str, str]:
    """Build the environment for an execute() subprocess.

    We start from the current process env (so PATH, HOME, LANG, etc.
    are preserved) and overlay CI-style defaults.  The LLM-visible
    subprocess always sees these, so 'vitest' alone runs once and
    exits instead of entering watch mode.
    """
    env = os.environ.copy()
    env.update(_CI_ENV_DEFAULTS)
    return env


# ── Process-group reaper ────────────────────────────────────────────
#
# When a shell command times out we cannot rely on killing the shell
# alone: backgrounded jobs (`npm run dev &`) and forked build tools
# (esbuild workers, npm install's node children) survive the shell's
# death and get re-parented to init, leaving dozens of orphans in the
# container.  To stop that, ``execute`` runs the child in a *new
# process group* (start_new_session=True) and this reaper sends
# SIGTERM to the whole group on timeout, escalating to SIGKILL if any
# process is still alive after a short grace period.

_TERM_GRACE_SECONDS = 2.0


def _reap_process_group(
    proc: subprocess.Popen, grace: float = _TERM_GRACE_SECONDS
) -> None:
    """TERM the whole process group of *proc*, then KILL survivors.

    Best-effort: safe to call even if the process has already exited
    (os.getpgid / killpg raise ProcessLookupError, which we swallow).
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


# ── Execute tool ────────────────────────────────────────────────────


class ExecuteInput(BaseModel):
    """Input schema for the ``execute`` tool.

    Note: ``timeout`` is intentionally NOT exposed. The harness owns it
    via the EXECUTE_TIMEOUT environment variable, default 90s, capped
    to 600 seconds. If a command genuinely needs longer (e.g. heavy
    pip install), prefix the command with `timeout 180 ...` or set
    EXECUTE_TIMEOUT in the environment.
    """

    command: str = Field(description="The shell command to run")
    working_directory: str = Field(default=".", description="Working directory")


@tool("execute", args_schema=ExecuteInput)
def execute(command: str, working_directory: str = ".") -> str:
    """Run a shell command with stdin closed and a hard timeout.

    The command runs with /dev/null as stdin so interactive prompts
    cannot block. A timeout (default 90s, capped at 600s) is enforced
    by the harness; the LLM cannot extend it from the tool args. To
    raise it for one command, the LLM can wrap with `timeout NNN ...`;
    to raise it for the whole session, set EXECUTE_TIMEOUT in env.
    CI-style environment variables are injected by default (CI=1,
    NO_COLOR=1, etc.) so test runners do not enter watch mode. Dev
    servers and explicit --watch invocations are rejected at the
    boundary.
    """
    if _is_dangerous(command):
        return f"Error: dangerous command blocked: {command}"

    is_watch, watch_reason = _is_watch_command(command)
    if is_watch:
        return (
            "REJECTED: the harness does not run watch/daemon commands — "
            + watch_reason
            + f"\nRejected command: {command}"
        )

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
        proc = subprocess.Popen(
            fixed_command,
            shell=True,
            cwd=working_directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            env=_build_env(),
            # New session → new process group rooted at /bin/sh. On
            # timeout we killpg() the whole group so backgrounded
            # grandchildren (esbuild workers, npm install's node
            # children, `sleep 60 &`) do not survive as orphans.
            start_new_session=True,
        )
    except Exception as e:
        return notice + f"Error: {e}"

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _reap_process_group(proc)
        # Drain any buffered output. By now the group is dead so
        # communicate() returns promptly; the bounded timeout is
        # purely defensive.
        try:
            stdout, stderr = proc.communicate(timeout=_TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
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
        _reap_process_group(proc)
        return notice + f"Error: {e}"

    output = ""
    if stdout:
        output += stdout
    if stderr:
        output += f"\n[stderr]\n{stderr}"
    if proc.returncode != 0:
        output += f"\n[exit code: {proc.returncode}]"

    max_chars = 10000
    if len(output) > max_chars:
        output = output[:max_chars] + f"\n... (truncated, {len(output)} total chars)"

    return (notice + output.strip()) or "(no output)"


SHELL_TOOLS = [execute]
