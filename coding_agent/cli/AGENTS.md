<!-- Parent: ../../AGENTS.md -->
# cli/

## Purpose
Claude Code 스타일 대화형 CLI. Rich + prompt-toolkit 기반 스트리밍 출력, 도구 호출 실시간 표시.

## Key Files
| File | Role |
|------|------|
| `app.py` | 메인 REPL 루프 — `astream_events`로 스트리밍, 슬래시 커맨드 처리, 작업 디렉토리 관리 |
| `display.py` | 출력 포맷팅 — 도구 호출(⚡), 위임(⇢), 메모리(💾), 에러(✗), 완료(✓) 아이콘 |

## For AI Agents
- `app.py`의 `_run_agent_streaming()`이 LangGraph `astream_events(version="v2")`를 사용한다.
- 스트리밍 실패 시 `_run_agent_simple()`로 폴백한다.
- 최종 응답에서 JSON 메모리 블록과 `tool_call` 잔여물은 regex로 제거된다.
- 슬래시 커맨드: `/memory`, `/agents`, `/events`, `/status`, `/exit`.
