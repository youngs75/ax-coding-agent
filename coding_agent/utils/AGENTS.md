<!-- Parent: ../../AGENTS.md -->
# utils/

## Purpose
반복 작업 유틸리티. Langfuse 트레이스 추출 등 운영/디버깅 도구를 제공한다.

## Key Files
| File | Role |
|------|------|
| `langfuse_trace_exporter.py` | Langfuse에서 세션/트레이스를 추출하여 Markdown으로 export. CLI로 직접 실행 가능 |

## For AI Agents
- `python -m coding_agent.utils.langfuse_trace_exporter --list-sessions` 으로 세션 목록 조회.
- `--session <id>` 또는 `--trace <id>`로 특정 대화 내용을 Markdown으로 추출 가능.
- 수동으로 2회 이상 반복하는 작업이 있으면 이 디렉토리에 유틸리티로 추가한다.
