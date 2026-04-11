<!-- Parent: ../../AGENTS.md -->
# subagents/

## Purpose
동적 SubAgent 수명주기 관리. 8상태 FSM, 런타임 역할 분석, 인스턴스 추적, 이벤트 로그를 제공한다.

## Key Files
| File | Role |
|------|------|
| `models.py` | `SubAgentStatus` enum(8상태) + `VALID_TRANSITIONS` + `SubAgentInstance`/`SubAgentEvent`/`SubAgentResult` dataclass |
| `registry.py` | `SubAgentRegistry` — 인스턴스 생성/상태 전이/이벤트 로그/정리. 불법 전이는 경고 후 무시 |
| `factory.py` | `SubAgentFactory` — LLM으로 태스크를 분석하여 역할/도구/모델 결정, ROLE_TEMPLATES에서 시스템 프롬프트 생성 |
| `manager.py` | `SubAgentManager` — spawn(생성→실행→결과), cancel, cleanup. 실패 시 max_retries까지 재시도 |

## For AI Agents
- 상태 전이: CREATED→ASSIGNED→RUNNING→COMPLETED→DESTROYED. FAILED→ASSIGNED(재시도) 가능.
- `VALID_TRANSITIONS` dict로 불법 전이를 방어한다.
- `factory.py`의 `ROLE_TEMPLATES`에 5개 역할(planner/coder/reviewer/fixer/researcher)이 정의되어 있다.
- `manager.py`의 `_build_subagent_graph()`가 각 SubAgent를 독립 LangGraph로 실행한다.
- SubAgent의 `recursion_limit`은 `ainvoke(config={"recursion_limit": 500})`으로 전달된다.
