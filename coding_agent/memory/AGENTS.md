<!-- Parent: ../../AGENTS.md -->
# memory/

## Purpose
3계층 장기 메모리 시스템. 사용자 프로필, 프로젝트 컨텍스트, 도메인 지식을 SQLite+FTS5로 저장/검색/주입한다.

## Key Files
| File | Role |
|------|------|
| `schema.py` | `MemoryRecord` dataclass — id, layer, category, key, content, timestamps |
| `store.py` | `MemoryStore` — SQLite + FTS5 CRUD, upsert(UNIQUE on layer+project_id+key), 전문 검색 |
| `extractor.py` | `MemoryExtractor` — FAST 모델로 대화에서 메모리 자동 추출 (structured output JSON) |
| `middleware.py` | `MemoryMiddleware` — LangGraph 노드: inject(시스템 프롬프트에 `<agent_memory>` 주입) + extract_and_store(턴 종료 후 추출) |

## For AI Agents
- 3계층 분리: `user`(개인 선호), `project`(프로젝트별 규칙), `domain`(비즈니스 지식).
- `store.py`의 UNIQUE 제약: `(layer, project_id, key)` — 같은 키는 upsert로 덮어쓴다.
- `project_id`가 NULL일 때 SQLite의 NULL≠NULL 문제를 빈 문자열로 우회한다.
- FTS5 인덱스는 INSERT/UPDATE/DELETE 트리거로 자동 동기화된다.
