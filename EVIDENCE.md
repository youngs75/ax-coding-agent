# AX Coding Agent — 요구사항 증빙 문서

## 증빙 체크리스트 요약

| 항목 | 최소 증빙 | 상태 |
|------|----------|------|
| 장기 메모리 | 저장 구조 + read/write 시나리오 | **충족** |
| 동적 SubAgent | 생성/상태 전이/종료 로그 | **충족** |
| 루프 복원력 | timeout/retry/fallback/safe stop | **충족** |
| 모델 정책 | 사용 모델과 선택 이유 | **충족** |
| 대안 구현 정당화 | DeepAgents 기준과의 기능 매핑 | **충족** |

---

## 1. 장기 메모리와 지식 저장 체계

### 설계 의도

사용자가 반복 사용하면서 입력하는 지식이 누적되고, 다음 세션/작업에서 자동으로 재활용되는 구조.
단순 체크포인터나 대화 히스토리가 아닌, 3계층으로 분리된 구조화된 장기 메모리.

### 코드 위치

| 구성 요소 | 파일 | 핵심 라인 |
|----------|------|----------|
| 3계층 스키마 | `coding_agent/memory/schema.py` | L31: `layer: Literal["user", "project", "domain"]` |
| SQLite+FTS5 저장소 | `coding_agent/memory/store.py` | L78-251: MemoryStore 전체 |
| LLM 자동 추출 | `coding_agent/memory/extractor.py` | L56-101: extract() — 사용자 메시지에서 사실 추출 |
| 시스템 프롬프트 주입 | `coding_agent/memory/middleware.py` | L51-95: inject() — XML `<agent_memory>` 블록 |
| 세션 캐시 최적화 | `coding_agent/memory/middleware.py` | L97-113: 토픽 유사도 기반 재검색 |

### 메모리 계층별 상세

| 메모리 층 | 무엇을 저장 | 언제 저장 | 언제 조회 | 어디에 지속 | 정정 방법 |
|----------|-----------|----------|----------|-----------|----------|
| `user` | 개발자 선호, 코딩 스타일, 반복 피드백 | 사용자 입력 시 LLM 추출 | 매 턴 inject() | SQLite `memories` 테이블 | upsert (ON CONFLICT DO UPDATE) |
| `project` | 아키텍처 결정, 파일 구조, 기술 스택 | 사용자 입력 시 LLM 추출 | 매 턴 inject() (project_id 필터) | SQLite `memories` 테이블 | upsert (동일 key 덮어쓰기) |
| `domain` | 비즈니스 용어, 업무 규칙, API 계약 | 사용자 입력 시 LLM 추출 | 매 턴 FTS5 검색 (토픽 유사도) | SQLite `memories_fts` 가상 테이블 | upsert + `/memory delete` CLI |

### 충족 시나리오 (실제 로그)

```
# 사용자 입력 후 자동 추출 — .ax-agent/logs/agent.log에서 발췌
event='memory_extractor.extracted' count=11
event='memory_store.upserted' key='dev_methodology_sdd_tdd' layer='project'
event='memory_store.upserted' key='domain_business_roles' layer='domain'
event='memory_store.upserted' key='domain_core_feature_gantt' layer='domain'

# 다음 턴에서 메모리 주입
event='memory_middleware.injected' user=0 project=6 domain=0
```

사용자가 "TDD 방식으로 개발"이라고 입력 → `project` 계층에 `dev_methodology_sdd_tdd` 저장 → 이후 coder SubAgent에게 위임 시 시스템 프롬프트에 주입됨.

### 테스트 증빙

```bash
make test-memory  # 10개 테스트
# test_upsert_and_get, test_search_fts, test_three_layer_separation 등
```

---

## 2. 동적 SubAgent 수명주기 관리

### 설계 의도

미리 고정된 역할이 아닌, 작업 성격에 따라 런타임에 SubAgent를 생성하고, 상태를 추적하고, 정리하는 구조.
Claude Code의 Coordinator 패턴 + DeepAgents의 "call once, return control" 원칙을 결합.

### 코드 위치

| 구성 요소 | 파일 | 핵심 라인 |
|----------|------|----------|
| 8상태 FSM | `coding_agent/subagents/models.py` | L12-58: SubAgentStatus + VALID_TRANSITIONS |
| 메타데이터 | `coding_agent/subagents/models.py` | L70-110: SubAgentInstance dataclass |
| 동적 생성 팩토리 | `coding_agent/subagents/factory.py` | L143-184: create_for_task() |
| 키워드 분류 | `coding_agent/subagents/factory.py` | L219-254: _ROLE_KEYWORDS + _analyze_task() |
| 상태 전이 + 이벤트 로그 | `coding_agent/subagents/registry.py` | L76-127: transition_state() |
| 수명주기 관리 | `coding_agent/subagents/manager.py` | L57-198: spawn() + _execute_with_retries() |
| 컨텍스트 격리 | `coding_agent/subagents/manager.py` | L200-246: _run_agent() — 독립 그래프 |
| 조기 종료 감지 | `coding_agent/subagents/manager.py` | L269-300: 반복 도구 호출 감지 |
| Orchestrator 위임 | `coding_agent/tools/task_tool.py` | L66-84: build_task_tool() |

### 상태 전이 다이어그램

```
CREATED → ASSIGNED → RUNNING → COMPLETED → DESTROYED
                       ↓ ↑         ↓
                    BLOCKED    FAILED → ASSIGNED (retry, max 2회)
                       ↓              → DESTROYED (포기)
                    CANCELLED → DESTROYED
```

### 역할 템플릿 (5종)

| 역할 | 모델 티어 | 도구 | 용도 |
|------|----------|------|------|
| `planner` | reasoning | read, write, glob, grep | PRD/SPEC 문서 작성 |
| `coder` | strong | read, write, edit, execute, glob, grep | 코드 생성, TDD |
| `reviewer` | default | read, glob, grep | 코드 리뷰 |
| `fixer` | strong | read, edit, execute, grep | 버그 수정 |
| `researcher` | default | read, glob, grep | 기술 조사 |

### 충족 시나리오 (실제 로그)

```
# SubAgent 동적 생성 + 상태 전이 — .ax-agent/logs/agent.log
event='subagent.created' agent_id='s-51jh8tgo' role='planner' model_tier='reasoning'
event='subagent.transition' agent_id='s-51jh8tgo' from_state='created' to_state='assigned'
event='subagent.transition' agent_id='s-51jh8tgo' from_state='assigned' to_state='running'
event='timing.subagent.invoke' agent_id='s-51jh8tgo' invoke_s=60.584 msg_count=10
event='subagent.transition' agent_id='s-51jh8tgo' from_state='running' to_state='completed'
event='subagent.transition' agent_id='s-51jh8tgo' from_state='completed' to_state='destroyed'
event='timing.task_tool.done' success=True duration_s=60.6 files=1
```

### 실패 + 재시도 시나리오 (실제 로그)

```
# SPEC 작성 중 timeout → 재시도
event='timing.subagent.invoke_error' agent_id='s-tovb9gav' elapsed_s=78.64 error='timed out'
event='subagent.transition' from_state='running' to_state='failed' reason='timed out'
event='subagent.retry' agent_id='s-tovb9gav' attempt=1 max_retries=2
event='subagent.transition' from_state='failed' to_state='assigned' reason='preparing'
event='subagent.transition' from_state='assigned' to_state='running' reason='starting'
```

### 테스트 증빙

```bash
make test-subagents  # 14개 테스트
# test_full_lifecycle, test_retry_lifecycle, test_blocked_lifecycle 등
```

---

## 3. Agentic Loop 복원력과 안전성

### 설계 의도

"생각 → 도구 사용 → 결과 반영 → 다음 행동 결정" 루프가 멈추거나 깨질 때의 방어 전략.
7가지 장애 유형 모두에 대해 감지 → 재시도 → 폴백 → 안전 중단 정책을 정의.

### 코드 위치

| 구성 요소 | 파일 | 핵심 라인 |
|----------|------|----------|
| Watchdog (타임아웃) | `coding_agent/resilience/watchdog.py` | 전체: asyncio timeout 기반 |
| 에러 분류 | `coding_agent/resilience/retry_policy.py` | L22-116: FailureType + DEFAULT_POLICIES |
| 진전 감시 | `coding_agent/resilience/progress_guard.py` | 전체: 동일 액션 반복 감지 |
| 안전 중단 | `coding_agent/resilience/safe_stop.py` | L37-108: 조건 평가 |
| 통합 에러 처리 | `coding_agent/resilience/error_handler.py` | L28-175: retry/fallback/abort 결정 |
| 연속 에러 한도 | `coding_agent/core/loop.py` | L320: _MAX_CONSECUTIVE_ERRORS = 3 |
| Resume 기능 | `coding_agent/core/loop.py` | L539-627: 중단 시 resume.json 저장 |

### 장애 유형별 처리 행렬

| 장애 유형 | 감지 신호 | 허용 재시도 | fallback | 사용자 노출 상태 | safe stop 조건 | 코드 위치 |
|----------|----------|-----------|---------|---------------|---------------|----------|
| 모델 무응답/지연 | asyncio.TimeoutError | 2회 | 하위 티어 모델 | `재시도 중` | 재시도 한도 초과 | `watchdog.py` |
| 반복 무진전 루프 | 3회 동일 액션 | 0 | 전략 변경 | `진전 없음 감지` | 전략 전환 후에도 무진전 | `progress_guard.py` |
| 잘못된 tool call | JSON 파싱 실패 | 1회 | 프롬프트 기반 폴백 | `도구 호출 수정 중` | 동일 오류 반복 | `tool_call_utils.py` |
| SubAgent 실패 | FAILED 상태 전이 | 역할별 2회 | 다른 역할 SubAgent | `하위 작업 실패` | 대체 경로도 실패 | `manager.py` |
| 외부 API 오류 | 4xx/5xx, 네트워크 | 3회 | 대체 모델 | `외부 서비스 오류` | 재시도 비용 과도 | `retry_policy.py` |
| 모델 폴백 필요 | 컨텍스트 초과 | 0 | REASONING→STRONG→DEFAULT→FAST | `모델 전환 중` | 모든 모델 소진 | `error_handler.py` |
| 안전 중단 필요 | max_iterations, 위험 경로 | 0 | 없음 | `안전하게 중단됨` | 즉시 중단 | `safe_stop.py` |

### 폴백 체인

```python
# coding_agent/models.py L162
FALLBACK_ORDER: list[TierName] = ["reasoning", "strong", "default", "fast"]
```

### 충족 시나리오 (실제 로그)

```
# 연속 에러 감지 → 즉시 중단
event='error_handler.consecutive_limit' count=3 error='timed out'

# 모델 폴백
event='error_handler.resolution' action='fallback' status='모델 전환: strong → default'

# Resume 기능 — safe_stop 후 이어서 작업
event='resume_state.saved' path='/workspace/.ax-agent/resume.json'
```

### 테스트 증빙

```bash
make test-resilience  # 21개 테스트
# test_timeout, test_timeout_with_callback, test_ok_on_normal, test_warn_on_stall,
# test_stop_on_max_iterations, test_retry_decision, test_fallback_after_retries,
# test_abort_when_no_fallback, test_korean_status_messages 등
```

---

## 4. 모델 정책

### 사용 모델

| 티어 | 모델 | 프로바이더 | 용도 |
|------|------|----------|------|
| **REASONING** | qwen3-max | DashScope (직접) | 계획, 아키텍처 설계, PRD/SPEC 작성 |
| **STRONG** | qwen3-coder-next | DashScope (직접) | 코드 생성, 도구 호출, TDD 구현 |
| **DEFAULT** | qwen3.5-plus | DashScope (직접) | 분석, 검증, 코드 리뷰 |
| **FAST** | qwen3.5-flash | DashScope (직접) | 파싱, 분류, 메모리 추출 |

모든 모델은 **오픈소스 Qwen 계열**이며, DashScope API를 통해 직접 호출합니다.
LiteLLM Proxy를 경유하여 Langfuse로 자동 트레이싱됩니다.

### 모델 선택 이유

1. **Qwen 계열**: tool calling 지원이 안정적, DashScope에서 직접 호출 가능
2. **4-Tier 분리**: 작업 복잡도에 맞는 모델 투입으로 비용 최적화
3. **DashScope 직접 호출**: OpenRouter 경유 대비 안정성 확보 (네트워크 에러 제거)

### 오픈소스 모델 호환성 처리

| 호환성 문제 | 해결 방법 | 코드 위치 |
|------------|----------|----------|
| tool calling 미지원 | 프롬프트 기반 폴백 | `core/tool_adapter.py` |
| JSON args 파싱 오류 | 3단계 복구 (정규식 → JSON 재파싱 → 부분 매칭) | `core/tool_call_utils.py` |
| tool_choice 미지원 | 자동 감지 후 비활성화 | `models.py` _NO_TOOL_CHOICE |
| DashScope 직렬화 | additional_kwargs.tool_calls 변환 | `core/tool_call_utils.py` |

### 증빙

- `.env`: REASONING_MODEL, STRONG_MODEL, DEFAULT_MODEL, FAST_MODEL
- `litellm_config.yaml`: 모든 모델 라우팅 설정
- Langfuse 트레이스: 모델별 호출 횟수, 비용, 지연 시간 확인 가능

---

## 5. DeepAgents 기준과의 기능 매핑

| DeepAgents 구성 요소 | AX Agent 대응 | 코드 위치 |
|---------------------|-------------|----------|
| `MemoryMiddleware` | `MemoryMiddleware` (inject/extract) | `memory/middleware.py` |
| `SubAgentMiddleware` | `SubAgentManager` + `task` 도구 | `subagents/manager.py`, `tools/task_tool.py` |
| `start_async_task()` | `task()` 도구 (StructuredTool) | `tools/task_tool.py` |
| `<agent_memory>` 태그 | `<agent_memory>` XML 블록 | `memory/middleware.py` _build_xml() |
| 미들웨어 체인 | LangGraph 노드 체인 | `core/loop.py` _build_graph() |
| 3가지 SubAgent 타입 | 5가지 역할 템플릿 | `subagents/factory.py` ROLE_TEMPLATES |
| _EXCLUDED_STATE_KEYS | 독립 그래프 (컨텍스트 격리) | `subagents/manager.py` _run_agent() |

### 추가 차별화 (DeepAgents에 없는 것)

| 기능 | 설명 | 코드 위치 |
|------|------|----------|
| 8상태 FSM | BLOCKED, CANCELLED 포함한 완전한 상태 머신 | `subagents/models.py` |
| 이벤트 로그 | SubAgent 전체 생명주기 기록 | `subagents/registry.py` |
| 도구 결과 캐싱 | read_file/glob/grep 결과 캐시, write 시 무효화 | `tools/file_ops.py` _ToolCache |
| 메모리 검색 캐시 | 토픽 유사도 기반 domain 재검색 | `memory/middleware.py` _get_domain_cached() |
| 모델 인스턴스 캐시 | (tier, temperature) 키로 ChatOpenAI 재사용 | `models.py` _model_instance_cache |
| 조기 종료 감지 | 반복 도구 호출 3회 시 자동 중단 | `subagents/manager.py` should_continue() |
| Resume 기능 | safe_stop 시 resume.json 저장, /resume로 이어서 | `core/loop.py` _save_resume_state() |
| 타이밍 계측 | 모든 노드/SubAgent/도구 호출에 소요 시간 기록 | `core/loop.py`, `subagents/manager.py` |

---

## 6. 성능 프로파일링 결과

### 병목 분석 (실측 데이터)

| 구간 | 최적화 전 | 최적화 후 | 개선 |
|------|----------|----------|------|
| extract_memory (매 턴) | 5~12초/턴 × 7턴 = **47초** | 사용자 입력 시 1회 = **~5초** | **-42초** |
| SubAgent 분류 LLM 호출 | 1~5초/회 | 키워드 매칭 0ms | **~95% 제거** |
| 모델 인스턴스 재생성 | ~200ms/회 | 캐시 재사용 | **제거** |
| ThreadPool 재생성 | ~50ms/회 | 공유 풀 | **제거** |
| OpenRouter 네트워크 에러 | 간헐 120초 대기 | DashScope 직접 호출 | **제거** |

### Langfuse 트레이스 검증

```bash
# 트레이스 추출 유틸리티
python -m coding_agent.utils.langfuse_trace_exporter --list-traces 10
python -m coding_agent.utils.langfuse_trace_exporter --trace <trace-id> -v
```

---

## 7. 최종 자기 점검

| # | 질문 | 답변 | 근거 |
|---|------|------|------|
| 1 | user/profile, project/context, domain/knowledge를 구분하는 장기 메모리 설계가 있는가? | **예** | `schema.py` L31: 3계층 Literal 타입 |
| 2 | 사용자가 새 도메인 지식을 입력하면 이후 작업에서 그 지식을 재사용하는가? | **예** | `middleware.py` inject(): FTS5 검색 → XML 주입 |
| 3 | SubAgent는 런타임에 생성되고, 상태 전이와 종료가 기록되는가? | **예** | `factory.py` create_for_task() + `registry.py` 이벤트 로그 |
| 4 | SubAgent가 실패하거나 blocked 되었을 때의 처리 규칙이 있는가? | **예** | `manager.py` _execute_with_retries(): max_retries=2 |
| 5 | LLM 실패 시 retry, fallback, safe stop 중 무엇을 할지 정의되어 있는가? | **예** | `error_handler.py`: 7가지 장애 유형별 정책 |
| 6 | 안전하게 멈추는 기준이 있는가? | **예** | `safe_stop.py` + 연속 에러 3회 한도 |
| 7 | DeepAgents 동등 역량 설명이 가능한가? | **예** | 위 매핑 테이블 참조 |
| 8 | 오픈소스 모델 사용과 이유를 명시했는가? | **예** | Qwen 계열 4종, DashScope 직접 호출 |
| 9 | 기존 CRUD 실습과 차이를 설명할 수 있는가? | **예** | Agentic 오케스트레이션 프레임워크 (CRUD 아님) |

---

## 8. 아직 남아 있는 한계

1. **git worktree 기반 병렬 실행**: 설계 완료, 미구현. 현재 순차 실행만 지원.
2. **메모리 정정 UI**: `/memory delete`로 삭제 가능하나, 충돌 시 자동 정정 로직은 미구현.
3. **HITL (Human-in-the-Loop)**: Planner 계획 승인 후 실행하는 interrupt 미구현.
4. **프론트엔드 산출물**: PMS E2E 테스트 시 백엔드 위주로 생성됨. 프론트엔드는 추가 실행 필요.

---

## 9. 테스트 실행

```bash
# 전체 테스트 (65개)
make test

# 모듈별 테스트
make test-memory       # 메모리 시스템 (10개)
make test-subagents    # SubAgent 상태 전이 (14개)
make test-resilience   # 복원력 (21개)

# 성능 최적화 테스트 (18개) — tests/test_performance.py
python -m pytest tests/test_performance.py -v
```
