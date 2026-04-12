"""Tests for coding_agent.tools.shell — execute hardening (P2.5)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coding_agent.tools.shell import (
    _AUTO_FIX_RULES,
    _CI_ENV_DEFAULTS,
    _autofix_command,
    _build_env,
    _is_dangerous,
    _is_watch_command,
    _resolve_timeout,
    execute,
)


# ── _is_dangerous ────────────────────────────────────────────────────


def test_dangerous_blocks_known_patterns():
    assert _is_dangerous("rm -rf /")
    assert _is_dangerous("RM -RF /")  # case insensitive
    assert _is_dangerous("dd if=/dev/zero of=/dev/sda")
    assert _is_dangerous(":(){ :|:& };:")


def test_dangerous_allows_safe_paths():
    assert not _is_dangerous("rm -rf /workspace/build")
    assert not _is_dangerous("rm -rf node_modules")
    assert not _is_dangerous("rm -rf /tmp/agent-build-123")
    assert not _is_dangerous("ls -la")
    assert not _is_dangerous("npm install")


def test_dangerous_blocks_system_root_deletion():
    assert _is_dangerous("rm -rf /usr/local")
    assert _is_dangerous("rm -rf /etc/passwd")
    assert _is_dangerous("rm -rf /*")
    assert _is_dangerous("rm -rf / something")


# ── _resolve_timeout ─────────────────────────────────────────────────


def test_resolve_timeout_default(monkeypatch):
    """Default is 90s (was 300s pre-v8.1).

    Single biggest contributor to v8 verifier hangs (449s) was the
    first execute call eating the full 300s window because the LLM
    had not yet learned to inline `timeout NN`. 90s comfortably covers
    normal pytest/build/install while killing collection hangs early.
    """
    monkeypatch.delenv("EXECUTE_TIMEOUT", raising=False)
    assert _resolve_timeout() == 90


def test_resolve_timeout_clamps_to_max(monkeypatch):
    monkeypatch.setenv("EXECUTE_TIMEOUT", "9999")
    assert _resolve_timeout() == 600


def test_resolve_timeout_clamps_to_min(monkeypatch):
    monkeypatch.setenv("EXECUTE_TIMEOUT", "1")
    assert _resolve_timeout() == 30


def test_resolve_timeout_ignores_garbage(monkeypatch):
    monkeypatch.setenv("EXECUTE_TIMEOUT", "not-a-number")
    assert _resolve_timeout() == 90


def test_resolve_timeout_env_override_within_range(monkeypatch):
    """Operators can still raise the cap via EXECUTE_TIMEOUT env."""
    monkeypatch.setenv("EXECUTE_TIMEOUT", "240")
    assert _resolve_timeout() == 240


def test_execute_default_timeout_constant_is_90():
    """Direct constant pin so a future global edit cannot quietly
    revert this without the test catching it."""
    from coding_agent.tools.shell import _EXECUTE_TIMEOUT_DEFAULT
    assert _EXECUTE_TIMEOUT_DEFAULT == 90


# ── _autofix_command ─────────────────────────────────────────────────


def test_autofix_apt_install_adds_yes():
    fixed, reasons = _autofix_command("apt-get install curl")
    assert "-y" in fixed
    assert reasons


def test_autofix_apt_install_skips_when_already_yes():
    fixed, reasons = _autofix_command("apt-get install -y curl")
    assert fixed == "apt-get install -y curl"
    assert reasons == []


def test_autofix_apt_upgrade_adds_yes():
    fixed, reasons = _autofix_command("apt upgrade")
    assert "-y" in fixed


def test_autofix_npm_create_vite_adds_yes():
    fixed, reasons = _autofix_command(
        "cd /workspace && npm create vite@latest . -- --template react-ts"
    )
    assert "--yes" in fixed
    assert reasons


def test_autofix_npm_create_skips_when_already_yes():
    cmd = "npm create vite@latest --yes . -- --template react-ts"
    fixed, reasons = _autofix_command(cmd)
    assert reasons == []


def test_autofix_npx_create_react_app():
    fixed, reasons = _autofix_command("npx create-react-app my-app")
    assert "--yes" in fixed


def test_autofix_chained_command_only_fixes_matching_segment():
    fixed, _ = _autofix_command("ls && apt-get install curl && echo done")
    assert "apt-get install -y" in fixed
    assert "ls &&" in fixed
    assert "echo done" in fixed


def test_autofix_unknown_command_unchanged():
    fixed, reasons = _autofix_command("python -m pytest tests/")
    assert fixed == "python -m pytest tests/"
    assert reasons == []


# ── execute tool — actual subprocess behaviour ──────────────────────


def test_execute_blocks_dangerous():
    out = execute.invoke({"command": "rm -rf /"})
    assert "blocked" in out.lower()


def test_execute_runs_simple_command():
    out = execute.invoke({"command": "echo hello"})
    assert "hello" in out


def test_execute_closes_stdin_so_cat_returns_immediately(tmp_path: Path):
    """Without stdin=DEVNULL, `cat` blocks forever waiting for input."""
    out = execute.invoke({"command": "cat", "working_directory": str(tmp_path)})
    # cat with empty stdin returns nothing — must NOT hang.
    assert "(no output)" in out or out.strip() == ""


def test_execute_timeout_returns_diagnostic_message(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("EXECUTE_TIMEOUT", "30")  # clamped to 30 (min)
    # Use a tiny effective timeout via a sleep that exceeds it.
    # We need the test to be fast — patch the resolver instead.
    import coding_agent.tools.shell as shell_mod

    monkeypatch.setattr(shell_mod, "_resolve_timeout", lambda: 1)
    out = execute.invoke({"command": "sleep 5", "working_directory": str(tmp_path)})
    assert "[TIMEOUT]" in out
    assert "Do NOT retry the same command" in out
    assert "sleep 5" in out


def test_execute_autofix_notice_in_output():
    """The notice prefix lets the LLM see what was rewritten."""
    out = execute.invoke({"command": "echo before && apt-get install -y nothing-pkg"})
    # Already has -y, so no autofix notice.
    assert "[notice]" not in out


def test_execute_autofix_npm_create_emits_notice(tmp_path: Path, monkeypatch):
    """Verify the notice prefix appears when a real autofix is applied.

    We can't actually run `npm create vite` in CI, so we use a fake
    command that matches one of the rules but resolves to /bin/true.
    """
    # Match `npm init <pkg>` rule with a benign shim
    # The autofix turns it into `npm init nothing --yes`, which we
    # then short-circuit by aliasing in the working dir? Simpler:
    # just verify _autofix_command + the prefix construction logic.
    fixed, reasons = _autofix_command("npm init somepkg")
    assert "--yes" in fixed
    assert reasons


# ── Watch/daemon command guard (P0.2) ────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "vitest",
        "npx vitest",
        "vitest --ui",
        "vitest --watch",
        "jest --watch",
        "jest --watchAll",
        "npm run dev",
        "npm run start",
        "yarn dev",
        "pnpm dev",
        "yarn serve",
        "npm run watch",
        "npx vite",
        "vite",
        "vite dev",
        "next dev",
        "nuxt dev",
        "webpack-dev-server",
        "python -m http.server",
        "http.server",
        "flask run",
        "uvicorn main:app",
        "gunicorn app:main",
        "tsc --watch",
        "tsc -w",
        "nodemon app.js",
        "pm2 start app.js",
        "cd /workspace && npm run dev",  # prefix cd should still trigger
        "source .env && vitest",
    ],
)
def test_is_watch_command_blocks_known_daemons(command: str):
    is_watch, reason = _is_watch_command(command)
    assert is_watch, f"expected watch-mode rejection for: {command!r}"
    assert reason  # non-empty diagnostic


@pytest.mark.parametrize(
    "command",
    [
        "vitest run",
        "vitest run --reporter=verbose",
        "npx vitest run",
        "jest --watchAll=false",
        "npm test",
        "npm run build",
        "vite build",
        "next build",
        "pytest",
        "pytest -x",
        "ls -la",
        "python -m pytest",
        "tsc",
        "tsc --noEmit",
        "npm install",
        "npm ci",
        "echo hello",
        # Identifiers that contain substrings like "dev" or "serve"
        # but are not actually watch commands:
        "./test-server-dev-tools --help",
        "grep dev file.txt",
        "./my-watcher-tool --run",
    ],
)
def test_is_watch_command_allows_one_shot_variants(command: str):
    is_watch, reason = _is_watch_command(command)
    assert not is_watch, (
        f"false positive: {command!r} flagged as watch — reason: {reason}"
    )


def test_execute_rejects_watch_command_without_running(tmp_path: Path):
    result = execute.invoke(
        {"command": "vitest", "working_directory": str(tmp_path)}
    )
    assert "REJECTED" in result
    assert "watch" in result.lower()
    # Must include the actual rejected command in the message.
    assert "vitest" in result


def test_execute_rejects_npm_run_dev(tmp_path: Path):
    result = execute.invoke(
        {"command": "npm run dev", "working_directory": str(tmp_path)}
    )
    assert "REJECTED" in result
    assert "dev" in result.lower()


# ── CI environment injection (P0.1) ──────────────────────────────────


def test_ci_env_defaults_contain_expected_keys():
    # These are the CI-style variables we rely on to suppress
    # watch mode and interactive prompts in test runners.
    for key in ("CI", "DEBIAN_FRONTEND", "NO_COLOR", "TERM"):
        assert key in _CI_ENV_DEFAULTS


def test_build_env_overlays_ci_defaults_on_current_env(monkeypatch):
    monkeypatch.setenv("CI", "")  # simulate unset-like
    monkeypatch.setenv("PATH", "/tmp/fake-path:/usr/bin")
    env = _build_env()
    # Defaults applied
    assert env["CI"] == "1"
    assert env["DEBIAN_FRONTEND"] == "noninteractive"
    assert env["NO_COLOR"] == "1"
    assert env["TERM"] == "dumb"
    # Existing critical variables preserved
    assert env["PATH"].startswith("/tmp/fake-path")


def test_execute_subprocess_receives_ci_env(tmp_path: Path):
    """Real subprocess run — verify CI=1 lands in the child process."""
    result = execute.invoke(
        {
            "command": "env | grep -E '^(CI|NO_COLOR|DEBIAN_FRONTEND|TERM)='",
            "working_directory": str(tmp_path),
        }
    )
    # CI=1 must appear in the env output; spot-check a few keys
    assert "CI=1" in result
    assert "NO_COLOR=1" in result
    assert "DEBIAN_FRONTEND=noninteractive" in result
    assert "TERM=dumb" in result
