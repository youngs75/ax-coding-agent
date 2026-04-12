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

## 8. 실제 E2E 실행 증빙 (PMS 프로젝트 생성)

### 시나리오
단일 사용자 요청 (PMS 시스템 — PRD → SPEC → TDD 구현)으로 여덟 차례 E2E 실행을 수행하고, 매 실행마다 근본 원인 수정을 반영했다.

### 실행 이력

| # | 모델 | 환경 | 결과 | 핵심 개선 |
|---|------|------|------|---------|
| 1차 | DashScope Qwen | 최초 빌드 | list_directory 오류 96회, 텍스트 반복 726회, safe_stop | 도구 목록 프롬프트 주입, Fork Rules |
| 2차 | DashScope Qwen | SubAgent 트림 | coder 1개 완료, 50턴 소진 | 트림 제거, INCOMPLETE 시그널 |
| 3차 | DashScope Qwen | Fork Rules 수정 | 완료, 60개 파일, safe_stop | SubAgent 턴 제어 |
| 4차 | DashScope Qwen | Orchestrator 도구 제한 | **14.7분 완료**, 33개 파일, SPEC 7/7 작업 구현 | Orchestrator에 write_file/execute 차단 |
| 5차 | OpenRouter GLM-5 + Qwen | CLI 개선, 도구 제한 | ~35분, 100+ 파일, FS+BE 풀스택, 26+ 테스트 파일 | 프론트엔드/백엔드 동시 생성 |
| 6차 | DashScope Qwen3 직접 호출 | max_turns=100, LLM_TIMEOUT=600, 스피너 개선 | 24.8분, 16 SubAgent, 66 파일, 11 테스트, 자체 완료 보고서 생성 | 검증 사이클 완주, FINAL_REPORT.md 자동 생성 |
| 7차 | DashScope Qwen3 (Sub-B 직전) | `submit_spec_section` 4섹션 + per-task GWT 강제, reference example 첨부 | **무한 reject 루프** — 같은 잘못된 tasks 콘텐츠를 13회 연속 재전송 | 사용자 입력의 7섹션 SPEC 의도와 harness 4섹션 강제 충돌 발견 |
| **8차** | **DashScope Qwen3 (Sub-B + Phase 3 A/B/C)** | spec_tool 폐기, write_file SPEC 경로 거부 제거, todo ledger 자동 마킹, verifier 출력 강화, ProgressGuard task repeat | **PRD/SPEC 자율 작성 (25 atomic task), HITL 6문항, 무한 루프 0**, B-1 자동 ledger 작동 | **최종 제출 대상.** Harness 설계 철학 정립 — LLM에게 형식 강제 X, 안전·탐지·명료성·컨텍스트·관찰 5가지만 책임 |

### 5차 E2E 결과 (GLM5 기반)

**SubAgent 파이프라인** (10개 SubAgent, 29분):

| # | 역할 | 작업 | 시간 | 파일 |
|---|------|------|------|------|
| 1 | planner (reasoning) | PRD 작성 | 72.5s | 1 |
| 2 | planner (reasoning) | SPEC 작성 (DB 스키마) | 141.6s | 1 |
| 3 | coder (strong) | 백엔드 초기화 + DB 스키마 (TDD) | 378.6s | 29 |
| 4 | coder (strong) | 프로젝트 CRUD API | 190.3s | 7 |
| 5 | coder (strong) | 사용자 관리 API | 176.9s | 16 |
| 6 | coder (strong) | 간트 차트 API | 155.7s | 8 |
| 7 | coder (strong) | 프론트엔드 초기화 + 목록 페이지 | 209.3s | 33 |
| 8 | coder (strong) | 프로젝트 상세/생성/수정 폼 | 473.1s (50턴) | 5 |
| 9 | coder (strong) | 간트 차트 컴포넌트 | 325.1s | 6 |

**총 100+ 파일 생성** (backend + frontend + docs + 26개 테스트)

**아키텍처 증빙**:
- 4-Tier 모델 자동 활용: `reasoning=GLM5`, `strong=GLM5`, `default=qwen3-coder`, `fast=qwen3.5-flash`
- Orchestrator 직접 도구 호출 0회 (전부 SubAgent 위임)
- max_turns 도달 1회 (coder #8) — 이후 100으로 상향
- 텍스트 누출 0회 (`final_content` 매 iteration 리셋)
- CLI 트리 구조로 위임 계층 실시간 가시화

### 산출물 품질 (5차 E2E)

```
new_pms_glm/
├── docs/
│   ├── PRD.md    (18.7KB, 590줄)
│   └── SPEC.md   (11.5KB, 8개 테이블 ERD + 인덱스)
├── backend/      (NestJS + Prisma + PostgreSQL)
│   ├── prisma/
│   ├── src/
│   │   ├── controllers, services, routes, models, middleware
│   │   ├── tests/ (15+ 테스트 파일)
│   └── package.json, Dockerfile
├── frontend/     (React + Vite + TypeScript)
│   ├── src/
│   │   ├── components, pages, hooks, services, utils
│   │   ├── tests/ (10+ 테스트 파일)
│   └── package.json, vite.config.ts
└── docker-compose.yml
```

### 6차 E2E 상세 (주력 제출 대상)

**구성**:
- REASONING/STRONG/DEFAULT/FAST 모두 DashScope 직접 호출
- `qwen3-max`, `qwen3-coder-next`, `qwen3.5-plus`, `qwen3.5-flash`
- 병렬로 z.ai GLM-5.1도 시도했으나 reasoning 모드 특성상 단일 LLM 호출이 600초+ → 타임아웃 → 중단

**SubAgent 파이프라인** (16개, 24.8분):

| # | 역할 | 작업 | 시간 | 파일 |
|---|------|------|------|------|
| 1 | planner | PRD 작성 | 42.3s | 1 |
| 2 | planner | SPEC 작성 | 85.4s | 1 |
| 3 | coder | 백엔드 구조 초기화 | 109.9s | **24** |
| 4 | coder | 프로젝트 CRUD API | 146.9s | 18 |
| 5 | coder | 프로젝트 조회 API | 64.9s | 0 |
| 6 | verifier | 환경 검증 | 13.5s | 0 |
| 7 | reviewer | 백엔드 리뷰 | 63.2s | 0 |
| 8 | fixer | 누락 기능 수정 | 28.0s | 0 |
| 9 | coder | 간트 차트 API (TDD) | 124.3s | 5 |
| 10 | coder | 프론트엔드 초기화 | 92.5s | **21** |
| 11 | coder | 프론트엔드 프로젝트 목록 | 50.7s | 1 |
| 12 | coder | 프론트엔드 간트 차트 | 20.0s | 0 |
| 13 | verifier | 통합 검증 | 3.9s | 0 |
| 14 | reviewer | 종합 코드 리뷰 | 115.0s | 0 |
| 15 | **fixer** | reviewer 이슈 수정 | **261.2s** | 1 |
| 16 | reviewer | 최종 품질 검증 | 85.9s | 0 |
| 17 | **planner** | **FINAL_REPORT.md 자동 생성** | 64.4s | 1 |

**지표 요약**:
- 총 시간: **24.8분** (1,485.8s)
- Orchestrator 반복: 23회 (max_iterations=50 내)
- **Orchestrator 직접 도구 호출: 0회**
- **max_turns 도달: 0회**
- **텍스트 누출: 0회**
- 실패한 SubAgent: 0개 (전원 success=True)
- Langfuse 트레이스: 100개, 평균 latency 5.34s, 총 비용 $0.37 (OpenRouter 부분만 계측)

**자체 검증 사이클 작동 증거**:
SubAgent #14(reviewer) → #15(fixer, 261s) → #16(reviewer 최종) → #17(planner FINAL_REPORT)
이 4단계 검증 체인이 자동으로 돌며, FINAL_REPORT.md에 **완료/부분완료/미완료 체크리스트**를 정직하게 기록.

### 6차 산출물 구조 (총 66 파일)

```
new_pms_qwen/
├── docs/
│   ├── PRD.md              (2.4KB)
│   ├── SPEC.md             (5.0KB, API-PROJ-01~05, API-GANTT-01~02, UI 명세)
│   ├── SETUP.md
│   ├── FINAL_REPORT.md     ← 자체 완료 보고서 (체크리스트 + 누락 사항)
│   └── api-spec/README.md
├── backend/                (Node.js + TypeScript + Express)
│   ├── src/
│   │   ├── controllers/    (project, gantt, user)
│   │   ├── services/       (project, gantt, user)
│   │   ├── routes/         (4개)
│   │   ├── models/         (project.entity, user.entity)
│   │   ├── utils/          (middleware, response)
│   │   └── server.ts
│   ├── __tests__/          ← 11개 테스트 파일 (project 5개, models, utils, health, etc.)
│   ├── db/                 (database.ts, schema.ts)
│   └── package.json, jest.config.js, tsconfig.json, README.md
├── frontend/               (Next.js + React + TypeScript + Tailwind)
│   ├── src/
│   │   ├── app/            (layout, page, gantt, projects)
│   │   ├── pages/          (_app, index, gantt, projects)
│   │   ├── components/     (common: Button/Input/Modal, layout)
│   │   ├── lib/api.ts
│   │   └── styles/globals.css
│   ├── package.json, tsconfig.json, tailwind.config.js
│   └── README.md
└── IMPLEMENTATION_SUMMARY.md
```

**SPEC 완료도** (FINAL_REPORT.md에 기록, 자체 평가):
| 기능 | 완료도 |
|------|-------|
| API-PROJ-01~05 (CRUD) | **100%** |
| API-GANTT-01 (조회) | **100%** |
| API-GANTT-02 (갱신) | 70% (순환 의존성 단순화) |
| UI-PROJ-LIST-01 | 60% (API 연동 미구현) |
| UI-GANTT-01 | 70% (API 연동 미구현) |
| UI-RESP-01 (반응형) | 20% (Tailwind 설정만) |

### 7차 E2E 회귀 사고 (Sub-B 전환 직전)

**구성**: DashScope Qwen3 직접 호출, `submit_spec_section` 4섹션(goals/tasks/dependencies/dod) + per-task GWT marker + 1200자 minimum + 25 dod checkbox + reference example 첨부.

**증상**: planner가 SPEC 작성 중 `tasks` 섹션을 13회 연속 동일한 잘못된 콘텐츠로 재전송하며 모두 `REJECTED` 받음. 단일 LLM 호출 82.8s, 54k input tokens, $0.11. orchestrator가 자연어 응답으로 종료하며 PRD만 남기고 SPEC 단계 실패.

**Langfuse trace 분석으로 발견한 근본 원인**:
1. **Task description과 도구 스키마의 구조 불일치** — orchestrator가 사용자 원본 입력의 "SPEC 7섹션 구조 (개요/아키텍처/데이터모델/API/테스트/구현/작업목록)"를 그대로 planner에 전달했는데, `submit_spec_section`은 4섹션만 받음. LLM은 `section: "tasks"`에 7섹션 잡탕을 욱여넣고 같은 잘못된 reject 메시지를 13회 받으면서도 self-correction 못 함.
2. **`_split_task_blocks` 카운팅 버그** — `_TASK_ID_PATTERN = r"TASK-\d{2,}"`가 본문 어디든 TASK-NN을 매칭해 별도 블록으로 자르는 바람에 dependencies 섹션 안의 cross-reference("TASK-01 → TASK-02")까지 짧은 블록으로 잡혀 100자 미달 reject.
3. **Reference example 첨부의 은밀한 bias** — planner 프롬프트에 PMS-스타일 SPEC 예시를 넣자 다른 도메인(ETL/게임 등)에도 PMS 4-tier 웹앱 구조를 끌고 올 위험이 표면화.

**의사결정**:
> "Harness로서 강화해야 하는 것은 LLM에게 패턴이나 포맷을 강제하는 것이 아니고, 주어진 역할에 맞게, 컨텍스트에 충실하게, LLM 스스로 알고있는 지식을 최대한 활용해 task를 정확하게 수행하라는 것이고, 오동작을 잘 탐지하고, 도구 호출에 명확한 정보와 명확한 응답을 해주는 것."

이 원칙에 따라 **Sub-B**(spec_tool 통째 폐기 + reference 미첨부 + planner 프롬프트 슬림화)와 **Phase 3 A/B/C**(verifier 출력 강화, ProgressGuard task repeat, 자동 todo 마킹) 두 패치를 8차 직전에 적용.

### 8차 E2E 상세 (최종 제출 대상)

**구성 변경 (7차 → 8차)**:
- `coding_agent/tools/spec_tool.py` 삭제 (4섹션 + per-task 검증 모두 제거)
- `coding_agent/tools/file_ops.py` `_check_write_policy`에서 SPEC 경로 거부 제거
- `coding_agent/subagents/factory.py` planner `default_tools`에서 `submit_spec_section` 제거, 프롬프트 슬림화 + HITL 1순위
- `coding_agent/core/loop.py` SYSTEM_PROMPT — submit_spec_section 가이드 제거, "사용자 명시 구조 그대로 전달" 명시, write_todos + 자동 마킹 가이드 추가
- `coding_agent/tools/todo_tool.py` 신규 — `TodoStore` + `write_todos` + `update_todo` (Claude Code TodoWriteTool 패턴)
- `coding_agent/tools/task_tool.py` — `_extract_task_id` + `manager.auto_advance_todo` (B-1 자동 마킹)
- `coding_agent/subagents/manager.py` — `_invoke_graph` verifier role 한정으로 execute(command, exit_code, stdout tail) 그대로 노출 (A-1)
- `coding_agent/resilience/progress_guard.py` — `_task_history` deque + `task_repeat_threshold=6`로 동일 TASK-NN 반복 차단 (A-2)
- `coding_agent/cli/display.py` `print_todo_panel` 추가 + spinner-safe 출력

**E2E 입력**: 7차와 동일 PMS 요구사항 (PM/관리자/웹·모바일/프로젝트 정보/Task 일정/간트 차트).

**실행 흐름** (관찰 시점까지):

| 단계 | SubAgent | 시간 | 결과 |
|------|----------|------|------|
| HITL Q1 | planner ask | - | 플랫폼: 반응형 웹 |
| HITL Q2 | planner ask | - | 간트: Frappe Gantt |
| HITL Q3 | planner ask | - | 인증: 기본 ID/PW |
| HITL Q4 | planner ask | - | 일정 항목: 단순 (이름/시작/종료) |
| 1 | planner | 45.5s · 2 steps · 2 tools | PRD.md 작성 |
| HITL Q1' | planner ask (SPEC 단계) | - | 백엔드: Python + FastAPI |
| HITL Q2' | planner ask (SPEC 단계) | - | DB: PostgreSQL |
| 2 | planner | 74.9s · 2 steps · 2 tools | SPEC.md 자율 작성 (**25 atomic task, 5 Phase**) |
| 3 | coder TASK-01 | 134.3s · 40 steps · 39 tools | Docker, Compose, CI/CD 구조 — todo 자동 ✓ |
| 4 | coder TASK-02 | (관찰 중) · - · - | PostgreSQL 컨테이너화 — todo 자동 ✓ |
| ... | (TASK-03/04 자동 진행) | | TASK-04까지 ledger ✓ 4/25 |
| 5 | coder TASK-05 | 325.1s · 100 steps · 106 tools | JWT 인증 API — INCOMPLETE (max_turns=100 도달) |
| 6 | verifier TASK-05 | 5.1s · 3 steps · 5 tools | 검증 결과 보고 |
| 7 | fixer TASK-05 | (관찰 중) | 누락 보강 |

**검증된 작동**:
- ✅ **B-1 자동 todo 마킹** — TASK-01~04가 ledger에 자동 ✓ 처리. orchestrator가 `update_todo`를 거의 호출하지 않음에도 panel이 정확히 갱신됨
- ✅ **Sub-B 자율 SPEC** — 25 atomic task를 5 Phase(인프라/인증/CRUD/간트/대시보드/마무리)로 LLM이 자율 구조화. 사용자 명시한 7섹션 형식 강제 없음에도 명확한 dependency 순서로 작성
- ✅ **HITL 2단계 분기** — PRD 단계에 4문항 → SPEC 단계에 백엔드/DB 2문항 추가. planner가 각 단계에 필요한 결정만 골라 사용자에게 질문하고, 답변이 `_user_decisions`로 누적되어 후속 SubAgent에 자동 prepend
- ✅ **C-2 순차 진행** — 7차에서 발생한 "TASK-04로 점프, TASK-01~03 건너뜀" 사고가 사라지고 TASK-01부터 정확히 순서대로 진행
- ✅ **무한 reject 루프 0** — 7차의 13회 reject 같은 사고 없음. spec_tool 자체가 폐기됐기 때문에 구조적으로 발생 불가능
- ✅ **CLI Rich Panel 실시간 갱신** — 25 task의 진행 상태(☐ pending / ◐ in_progress / ✓ completed)가 매 task delegation마다 자동 업데이트, spinner와 충돌 없음

**데이터 수집 (agent.log)**:
```
event='timing.agent_node' iteration=4 ... tool_calls=['write_todos']    # 1회 ledger 등록
event='timing.agent_node' iteration=6 ... tool_calls=['update_todo']    # 1회 수동 호출
event='timing.task_tool.start' agent_type='coder' desc='TASK-01: ...'
event='subagent.todo.auto_advance' task_id='TASK-01' status='in_progress'  # B-1 자동
event='subagent.todo.auto_advance' task_id='TASK-01' status='completed'     # B-1 자동
event='timing.task_tool.start' agent_type='coder' desc='TASK-02: ...'
... (TASK-02, 03, 04 동일 패턴)
```

LLM이 명시적으로 호출한 todo 도구는 `write_todos` 1회 + `update_todo` 1회뿐이고, **나머지 진행은 모두 harness가 자동 동기화**. 약한 모델(Qwen3)이 ledger 관리에 attention을 쓰지 않게 한 B-1의 효과가 실측으로 확인됨.

**재현 방법**:
```bash
make down && make up && ./ax-agent.sh /tmp/new_pms_qwen3_v8
# 동일 PMS 요구사항 입력 후 HITL 6문항 답변
```

### Claude 4.6 비교 실행 (참고, 중단)

Claude Opus + Sonnet 4.6 구성으로도 병행 테스트 시도 (`ax-agent-claude`):
- planner (Opus): PRD 290s → SPEC **530s (2,963줄, 12개 원자 태스크)** — Opus가 SDD 요구사항을 훨씬 정확히 이해
- coder #1 (Sonnet) TASK-001: 12분 경과 후 50턴 MAX (단일 태스크에 NestJS + Next.js 풀스택 초기화가 들어있어 과부하)
- 총 2개 Phase에 20분+ 소요, 마감 시간 제약으로 중단

### z.ai GLM-5.1 비교 실행 (참고, 중단)

z.ai BigModel API 직접 호출로 GLM-5.1 reasoning 모델 시도:
- planner PRD 250.5s — 완료
- planner SPEC **658초 후 LLM 단일 호출이 600초 timeout** → retry 2회 후 실패
- 원인: GLM-5.1이 reasoning 모델이라 thinking tokens이 단일 호출에서 600초를 초과
- Qwen3(DashScope)와의 속도 격차가 큼: PRD 42s vs 250s

**Reasoning 모델 공통 관찰** (Claude Opus, z.ai GLM-5.1):
- SPEC 작성 품질은 매우 높음 (Opus의 경우 12개 원자 태스크 분해)
- 하지만 단일 LLM 호출이 매우 길어 harness의 turn limit / timeout과 충돌
- **현재 harness는 non-reasoning 모델(Qwen3, OpenRouter GLM-5)과 더 잘 맞음**
- 다음 세션 개선 과제: harness 레벨 출력 구조화 (DeepAgents `write_todos` 패턴)로 weaker model의 SPEC 품질 보완

---

## 9. 아직 남아 있는 한계

**8차 세션에서 해결된 항목** (6차 시점에 한계로 기록되었던 것들):
- ~~HITL (Human-in-the-Loop)~~: ✅ `ask_tool.py` + LangGraph `interrupt()` 구현. 8차 E2E에서 PRD 4문항 + SPEC 2문항 답변이 후속 SubAgent에 자동 prepend됨
- ~~Harness 레벨 출력 구조화~~: ✅ `todo_tool.py`의 `write_todos` + B-1 자동 마킹으로 orchestrator의 진행 상황 추적 자동화. 단, **출력 형식 강제는 폐기**(Sub-B) — Harness 설계 철학 변경에 따른 의도된 결정

**남아 있는 한계**:
1. **git worktree 기반 병렬 실행**: 설계 완료, 미구현. 현재 순차 실행만 지원
2. **메모리 정정 UI**: `/memory delete`로 삭제 가능하나, 충돌 시 자동 정정 로직은 미구현
3. **모델 적응적 turn limit**: 현재 고정 `_SUBAGENT_MAX_TURNS=100`. 8차 E2E TASK-05(JWT 인증 API)에서 100턴 도달 후 INCOMPLETE 마킹 — 단일 task에 너무 많은 작업이 묶여 있을 가능성. 다음 세션 백로그: planner가 더 작은 단위로 분해하도록 가이드 (강제는 X)
4. **`_extract_task_id`의 SPEC ID 형식 의존성**: planner가 SPEC에 `TASK-NN` 형식 식별자를 안 쓰면 B-1 자동 마킹이 silent no-op. 다른 형식(예: `T01`, `Issue-1`)을 쓰는 SPEC에는 자동 동기화 안 됨. 패턴 확장 또는 LLM 기반 추출이 다음 백로그
5. **Stall watchdog 미구현**: Claude Code의 45s 프롬프트-패턴 기반 hang 감지 패턴 미적용. P0 shell hardening + ProgressGuard repeat 차단으로 8차에서 hang 0건 달성했지만, 향후 더 긴 작업에서는 stall watchdog이 보완책으로 필요할 수 있음
6. **단일 SubAgent 호출 비용 가시성**: Langfuse 트레이싱은 자동이지만 CLI에서 실시간 누적 비용 표시 없음

---

## 10. 테스트 실행

```bash
# 전체 테스트 (231개, 8차 세션 기준)
make test

# 모듈별 테스트
make test-memory       # 메모리 시스템
make test-subagents    # SubAgent 상태 전이
make test-resilience   # 복원력 (ProgressGuard task repeat 포함)

# 성능 최적화 테스트 — tests/test_performance.py
python -m pytest tests/test_performance.py -v

# 8차 세션 신규 테스트
python -m pytest tests/test_todo_tool.py -v          # Todo ledger 21개
python -m pytest tests/test_p35_phase3.py -v         # Phase 3 A/B/C 27개
python -m pytest tests/test_shell_tool.py -v         # P0 shell hardening 54개
```

**테스트 카운트 추이**:
| 단계 | 개수 | 신규 |
|------|------|------|
| 1차 제출 (5차 E2E) | 65 | 메모리 + SubAgent + 복원력 + 성능 |
| 6차 회귀 차단 (P3.5) | 145 | +80 (write_file 정책, decisions, role 분리) |
| 7차 P0 + Option A | 204 | +59 (shell hardening 54, fixer 도구 경계 3, spec 검증 2) |
| **8차 Sub-B + Phase 3 A/B/C** | **231** | **+27** (자동 todo 마킹, task repeat, verifier 출력) |

---

## 11. E2E 재현 절차

### 요구 사항
- Docker + Docker Compose
- `.env`에 OpenRouter 또는 DashScope API 키

### 실행

```bash
# 1. LiteLLM Gateway 기동 (OpenRouter/DashScope/Anthropic 통합)
docker compose up -d litellm

# 2. 에이전트 실행 (워크스페이스 지정)
./ax-agent.sh /path/to/new_workspace

# 3. 대화형 CLI에서 PMS 요청 입력
#    (PRD → SPEC → TDD 구현 프로세스 자동 수행)
```

### 결과물 검증

```bash
# 실제 생성된 파일 확인
find /path/to/new_workspace -not -path '*/node_modules/*' -type f | head -30

# SubAgent 실행 로그 확인
cat /path/to/new_workspace/.ax-agent/logs/agent.log | head -100

# Langfuse 트레이스 추출 (선택)
python -m coding_agent.utils.langfuse_trace_exporter --list-sessions 5
python -m coding_agent.utils.langfuse_trace_exporter --session <id> -v -o traces.md
```
