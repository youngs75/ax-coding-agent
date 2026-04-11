<!-- Parent: ../../AGENTS.md -->
# tools/

## Purpose
LangChain StructuredTool 기반 도구 시스템. 파일 조작, 셸 실행, SubAgent 위임 도구를 제공한다.

## Key Files
| File | Role |
|------|------|
| `file_ops.py` | `read_file`, `write_file`, `edit_file`, `glob_files`, `grep` — 파일 CRUD + 검색 |
| `shell.py` | `execute` — 셸 명령 실행, 위험 명령 차단, 타임아웃, 출력 크기 제한 |
| `task_tool.py` | `build_task_tool()` — SubAgent 위임 도구. 클로저로 `SubAgentManager`를 캡처 |

## For AI Agents
- `FILE_TOOLS`와 `SHELL_TOOLS` 리스트로 전체 도구를 export한다.
- `task_tool.py`의 `_run_task()`는 `asyncio.run()`과 `ThreadPoolExecutor` 폴백을 사용해 동기/비동기 컨텍스트 모두 지원한다.
- `shell.py`는 `rm -rf /`, `mkfs`, `fork bomb` 등 위험 명령을 차단한다.
