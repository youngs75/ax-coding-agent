"""셸 명령 실행 도구."""

from __future__ import annotations

import subprocess

from langchain_core.tools import tool
from pydantic import BaseModel, Field


class ExecuteInput(BaseModel):
    command: str = Field(description="실행할 셸 명령")
    working_directory: str = Field(default=".", description="작업 디렉토리")
    timeout: int = Field(default=30, description="타임아웃 (초)")


@tool("execute", args_schema=ExecuteInput)
def execute(command: str, working_directory: str = ".", timeout: int = 30) -> str:
    """셸 명령을 실행하고 stdout/stderr를 반환한다."""
    # 위험한 명령 차단
    dangerous = ["rm -rf /", "mkfs", "dd if=", ":(){", "fork bomb"]
    cmd_lower = command.lower()
    for d in dangerous:
        if d in cmd_lower:
            return f"Error: 위험한 명령이 차단되었습니다: {command}"

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=working_directory,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"

        # 출력 크기 제한
        max_chars = 10000
        if len(output) > max_chars:
            output = output[:max_chars] + f"\n... (truncated, {len(output)} total chars)"

        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: 명령 실행 타임아웃 ({timeout}초)"
    except Exception as e:
        return f"Error: {e}"


SHELL_TOOLS = [execute]
