<!-- Parent: ../AGENTS.md -->
# tests/

## Purpose
유닛 테스트. 3대 축(메모리, SubAgent, 복원력)을 각각 독립적으로 검증한다.

## Key Files
| File | Tests | Coverage |
|------|-------|----------|
| `test_memory.py` | 10개 | SQLite CRUD, FTS5 검색, 3계층 분리, upsert 덮어쓰기 |
| `test_subagents.py` | 14개 | 8상태 FSM 전이, 불법 전이 방어, 전체 수명주기, 재시도, blocked |
| `test_resilience.py` | 23개 | Watchdog timeout, ErrorClassifier, ProgressGuard stall 감지, SafeStop, ErrorHandler retry/fallback/abort |

## For AI Agents
- `pytest tests/ -v`로 전체 실행. 47개 전부 통과해야 한다.
- 메모리 테스트는 임시 SQLite DB를 사용 (`tempfile.mkstemp`).
- LLM 호출이 필요한 테스트는 없다 (모두 로컬에서 실행 가능).
