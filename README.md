# AX Coding Agent

3계층 장기 메모리, 동적 SubAgent 수명주기, Agentic Loop 복원력을 갖춘 AI Coding Agent Harness.

오픈소스 모델(Qwen 계열, GLM-5)과 폐쇄형 모델(Claude 4.6)을 모두 지원하며, 자체 LiteLLM Gateway + Langfuse 관측성을 통한 LLM 운영 체계를 포함합니다.

**실증된 역량** — PMS(Project Management System) 풀스택을 단일 사용자 요청으로 생성:
- **주력 (6차 E2E, Qwen3 DashScope 직접 호출)**: 24.8분 · 16 SubAgent · 66 파일 · 11 테스트 · FINAL_REPORT.md 자동 생성 · **max_turns 0회, 텍스트 누출 0회**
- **참고 (5차 E2E, OpenRouter GLM-5)**: 30분 · 10 SubAgent · 100+ 파일 · 26 테스트

자세한 내역은 [`EVIDENCE.md`](EVIDENCE.md)의 "8. 실제 E2E 실행 증빙" 참조.

---

## Docker 빌드 및 실행 방법

### 사전 요구사항

- Docker + Docker Compose
- API 키: DashScope (Qwen 권장, https://dashscope.aliyun.com/)

### 빠른 시작 (3단계)

```bash
# 1. 환경변수 설정 (API 키 입력)
cp .env.example .env && vi .env
#    → DASHSCOPE_API_KEY=sk-xxx 입력 후 저장

# 2. 빌드 + Gateway 기동 (이 명령 하나로 끝)
make up

# 3. 에이전트 대화형 실행
./ax-agent.sh [작업할_디렉토리_경로]
```

**`make up` 한 줄로 처리되는 것들**:
- `.env` 파일 존재 여부 검증 (없으면 안내 메시지 출력)
- `ax-coding-agent` Docker 이미지 빌드 (`docker-compose.yml`의 `agent` 서비스 `build: .` 사용)
- `litellm-db` (PostgreSQL), `litellm` (LLM Gateway) 기동
- LiteLLM health check 자동 대기 (최대 120초)
- 준비 완료 안내 메시지 출력

### 종료

```bash
make down            # 모든 컨테이너 정지
```

### 로그 확인

```bash
make logs            # LiteLLM Gateway 실시간 로그
```

### 저수준 명령 (고급 사용자용)

`make up`이 내부적으로 실행하는 것은 아래와 동등합니다:

```bash
docker compose up -d --build litellm-db litellm agent
curl http://localhost:4001/health/liveliness   # healthy 확인
./ax-agent.sh ~/my-project
```

**대안: docker compose로 agent 실행** (TTY 제한 있음, 비권장):
```bash
WORKSPACE_DIR=/path/to/project docker compose run --rm agent
```

---

## 핵심 아키텍처

```
┌─────────────────────────────────────────────────┐
│                   CLI (REPL)                     │
├─────────────────────────────────────────────────┤
│           Orchestrator (LangGraph)               │
│                                                  │
│  inject_memory → agent → tools → check_progress │
│       ↑              ↓           ↓               │
│       │        (SubAgent)   handle_error         │
│       │              ↓           ↓               │
│       └──────── continue ←── safe_stop → END    │
├─────────────────────────────────────────────────┤
│  Memory System  │  SubAgent System │ Resilience  │
│  ─────────────  │  ─────────────── │ ──────────  │
│  user/profile   │  Dynamic Factory │ Watchdog    │
│  project/context│  8-State FSM     │ RetryPolicy │
│  domain/knowl.  │  Registry+Events │ ProgressGrd │
│  SQLite+FTS5    │  6 Role Template │ SafeStop    │
│  Auto-Extract   │  Context Isolate │ ErrorHandler│
├─────────────────────────────────────────────────┤
│              Tools                               │
│  Orchestrator:                                   │
│    read_file │ glob_files │ grep │ task          │
│  SubAgent (역할별 조합):                          │
│    write_file │ edit_file │ execute │ (+ 위 4개) │
├─────────────────────────────────────────────────┤
│     LiteLLM Gateway → 멀티 프로바이더            │
│  REASONING │ STRONG │ DEFAULT │ FAST             │
│  (DashScope Qwen3 / OpenRouter / Anthropic)      │
│     + Langfuse 자동 트레이싱                     │
└─────────────────────────────────────────────────┘
```

**6 SubAgent 역할**: planner(reasoning) · coder(strong) · reviewer(default) · fixer(strong) · verifier(fast) · researcher(default)

**Orchestrator는 쓰기 도구가 없음** — `write_file`, `edit_file`, `execute`는 SubAgent 전용. 이 제약이 4차 E2E에서 Orchestrator 직접 도구 호출을 23회→0회로 감소시킨 핵심 개선.

### Orchestrator 패턴

Claude Code의 Coordinator 패턴 + DeepAgents의 "call once, return control" 원칙:

- Orchestrator는 **직접 코드를 작성하지 않음** — `task` 도구로 SubAgent에게 위임
- 각 SubAgent는 **독립된 컨텍스트**에서 실행 (컨텍스트 오염 방지)
- SubAgent 완료 후 Orchestrator가 결과를 검토하고 **다음 작업을 결정**
- 하나의 SubAgent에 과도한 작업을 주지 않고, **기능 단위로 분할 위임**

---

## 요구사항 매핑

### 1. 장기 메모리 (`coding_agent/memory/`)

| 요구사항 | 구현 위치 | DeepAgents 대응 |
|----------|----------|----------------|
| 3계층 분리 (user/project/domain) | `schema.py` L31 | `MemoryMiddleware` |
| 저장/조회/갱신/정정 | `store.py` MemoryStore (SQLite+FTS5, upsert) | AGENTS.md 파일 |
| 사용자 입력에서 자동 추출 | `extractor.py` MemoryExtractor (LLM) | 수동 |
| 시스템 프롬프트 주입 | `middleware.py` inject() → `<agent_memory>` XML | `wrap_model_call()` |
| 이후 작업에서 재사용 | inject → FTS5 검색 → 시스템 프롬프트 주입 | `<agent_memory>` 태그 |
| 세션 캐시 최적화 | `middleware.py` 토픽 유사도 기반 재검색 | 없음 |

### 2. 동적 SubAgent (`coding_agent/subagents/`)

| 요구사항 | 구현 위치 | DeepAgents 대응 |
|----------|----------|----------------|
| 런타임 동적 생성 | `factory.py` create_for_task() (키워드+LLM 분류) | `SubAgentMiddleware` |
| 8상태 FSM | `models.py` SubAgentStatus + VALID_TRANSITIONS | 3가지 SubAgent 타입 |
| 메타데이터 추적 | `models.py` SubAgentInstance (agent_id, role, state 등) | SubAgent TypedDict |
| 상태 전이 이벤트 로그 | `registry.py` SubAgentEvent | 없음 |
| 실패/재시도 처리 | `manager.py` _execute_with_retries() (max_retries=2) | 없음 |
| 컨텍스트 격리 | `manager.py` _run_agent() — 독립 LangGraph 인스턴스 | _EXCLUDED_STATE_KEYS |
| 조기 종료 감지 | `manager.py` should_continue() — 반복 도구 호출 3회 | 없음 |
| 정리 정책 | `registry.py` cleanup_completed() + _try_destroy() | 없음 |

### 3. Agentic Loop 복원력 (`coding_agent/resilience/`)

| 장애 유형 | 감지 | 재시도 | 폴백 | 구현 위치 |
|----------|------|--------|------|----------|
| 모델 무응답 | asyncio.TimeoutError | 2회 | 하위 티어 모델 | `watchdog.py` |
| 무진전 루프 | 3회 동일 액션 | 0 | 전략 변경 | `progress_guard.py` |
| 잘못된 tool call | JSON 파싱 실패 | 1회 | 프롬프트 기반 | `tool_call_utils.py` |
| SubAgent 실패 | FAILED 전이 | 2회 | 다른 역할 | `manager.py` |
| 외부 API 오류 | 4xx/5xx | 3회 | 대체 모델 | `retry_policy.py` |
| 모델 폴백 | 컨텍스트 초과 | 0 | REASONING→STRONG→DEFAULT→FAST | `error_handler.py` |
| 안전 중단 | max_iterations, 위험 경로, 연속 에러 3회 | 0 | 없음 | `safe_stop.py` |

---

## 모델 정책

### 프로바이더 선택 — DashScope 직접 호출 (기본)

**Qwen3 계열을 사용할 때는 DashScope를 직접 호출합니다.** OpenRouter 경유 시 잦은 provider timeout이 발생하는 문제를 E2E 실측으로 확인했기 때문입니다.

**OpenRouter 이슈 (실측)**:
- Qwen3 모델을 OpenRouter 경유로 호출 시 간헐적으로 요청이 응답 없이 hang되는 현상
- reviewer SubAgent(`openrouter/qwen/qwen3-coder-next`) 호출에서 14분 이상 응답 없음, watchdog `LLM_TIMEOUT=600` 초과
- Langfuse 트레이스에 요청은 등록되나 응답이 도착하지 않거나 `cost=$0`으로 기록
- OpenRouter 내부 upstream provider의 availability 이슈로 추정

**해결 — DashScope 직접 호출**:
- `DASHSCOPE_BASE_URL` 을 통해 Alibaba Cloud의 Qwen API를 직접 호출
- LiteLLM Proxy의 `model_name: dashscope/...` 라우팅 사용
- 네트워크 hop 하나 제거로 안정성 확보 (E2E 4차 실행에서 검증)
- Langfuse 트레이싱은 LiteLLM Proxy가 대신 수행

**GLM-5(OpenRouter)는 동일 이슈 없음**:
- z-ai의 GLM-5는 OpenRouter 경유로도 안정적으로 동작 (E2E 5차 검증)
- Qwen3 계열에 한해 OpenRouter 라우팅이 불안정

### 권장 구성 — DashScope Qwen3 전체 (6차 E2E 검증, 주력 제출)

**Qwen3 4-Tier 모두 DashScope 공식 API를 직접 호출**합니다. 이 구성이 6차 E2E에서 가장 안정적이고 생산적임을 확인했습니다.

| 티어 | 모델 | 프로바이더 | 용도 |
|------|------|-----------|------|
| REASONING | `qwen3-max` | **DashScope 직접** | 계획, 아키텍처 설계, PRD/SPEC 작성 |
| STRONG | `qwen3-coder-next` | **DashScope 직접** | 코드 생성, 도구 호출, TDD 구현 |
| DEFAULT | `qwen3.5-plus` | **DashScope 직접** | 분석, 검증, 코드 리뷰 |
| FAST | `qwen3.5-flash` | **DashScope 직접** | 파싱, 분류, 메모리 추출 |

**이 구성의 검증 결과 (6차 E2E)**:
- **총 24.8분**에 PMS 풀스택 완성 (백엔드 NestJS/Express + 프론트엔드 Next.js)
- **16 SubAgent** 파이프라인 완주 (planner → coder → verifier → reviewer → fixer 사이클)
- **66 파일 생성** (소스 + 11 테스트 파일 + 4 docs + FINAL_REPORT.md 자체 생성)
- **Orchestrator 직접 도구 호출 0회**, **max_turns 도달 0회**, **텍스트 누출 0회**
- **FINAL_REPORT.md 자동 생성** — 자체 완료도 체크리스트 포함 (API-PROJ 100%, UI 60-70%)
- **Langfuse 100 트레이스** (평균 5.3초 latency, 안정적)

**DashScope 직접 호출의 이점**:
- OpenRouter 경유 시 Qwen3의 provider timeout 이슈 회피 (위 "프로바이더 선택" 섹션 참조)
- Alibaba Cloud 인프라 내부에서 처리되어 응답 안정성 높음
- Langfuse 자동 트레이싱은 LiteLLM Proxy가 수행
- 국내(한국)에서 지리적으로 가까워 네트워크 hop 적음

### 대안 — OpenRouter GLM-5 (이전 권장)

| 티어 | 모델 | 용도 |
|------|------|------|
| REASONING | `openrouter/z-ai/glm-5` | 계획, 아키텍처 설계, PRD/SPEC 작성 |
| STRONG | `openrouter/z-ai/glm-5` | 코드 생성, 도구 호출, TDD 구현 |
| DEFAULT | `openrouter/qwen/qwen3-coder-next` | 분석, 검증, 코드 리뷰 (**timeout 위험**) |
| FAST | `openrouter/qwen/qwen3.5-flash-02-23` | 파싱, 분류, 메모리 추출 (**timeout 위험**) |

5차 E2E로 GLM-5 경로는 안정적 확인, 단 Qwen reviewer에서 14분 응답 없음 관찰. DEFAULT/FAST는 DashScope 직접 호출로 교체 권장.

### 대안 1 — DashScope Qwen 직접 호출 (Qwen 사용 시 필수)

| 티어 | 모델 | 용도 |
|------|------|------|
| REASONING | `dashscope/qwen3-max` | 계획, 아키텍처 설계 |
| STRONG | `dashscope/qwen3-coder-next` | 코드 생성 |
| DEFAULT | `dashscope/qwen3.5-plus` | 분석, 검증 |
| FAST | `dashscope/qwen3.5-flash` | 파싱, 분류 |

4차 E2E 검증 완료. 33개 파일 생성 / 백엔드 중심.

**중요**: Qwen3 계열은 반드시 DashScope 직접 호출(`dashscope/*`)을 사용해야 합니다. OpenRouter 경유 시 provider timeout이 자주 발생하여 SubAgent가 멈추는 현상이 있습니다(위 "프로바이더 선택" 섹션 참조).

### 대안 2 — Claude 4.6 (폐쇄형 최고 품질)

| 티어 | 모델 | 용도 |
|------|------|------|
| REASONING | `claude-opus-4-6` | 고품질 PRD/SPEC 작성 |
| STRONG | `claude-sonnet-4-6` | 코드 생성 |
| DEFAULT | `claude-sonnet-4-6` | 분석, 검증 |
| FAST | `claude-haiku-4-5` | 파싱, 분류 |

SDD 방식의 원자 단위 작업 분해는 Opus가 월등 (2,963줄 SPEC). 단 `LLM_TIMEOUT=600` 이상 필요 (Opus 단일 호출 180-300초).

### 모델 전환 방법

`.env`의 티어 변수만 교체 → Docker 재빌드:
```bash
# 권장: Zhipu GLM + DashScope Qwen 직접 호출
REASONING_MODEL=zhipu/glm-4.6
STRONG_MODEL=zhipu/glm-4.6
DEFAULT_MODEL=dashscope/qwen3-coder-next
FAST_MODEL=dashscope/qwen3.5-flash
```

또는 runtime에 `-e REASONING_MODEL=...`로 덮어쓰기 (`config.py`가 `override=False`로 dotenv 로드).

**공통 특징:**
- LiteLLM Proxy 경유 → Langfuse 자동 트레이싱
- tool calling 미지원 모델은 프롬프트 기반 폴백
- 4-Tier 분리로 작업 복잡도에 맞는 비용 최적화

**오픈소스 모델 호환성 처리:**
- tool calling 미지원 모델 → 프롬프트 기반 폴백 (`core/tool_adapter.py`)
- JSON args 파싱 오류 → 3단계 복구 (`core/tool_call_utils.py`)
- 폴백 체인: REASONING → STRONG → DEFAULT → FAST (`models.py`)

---

## 테스트 실행

```bash
# 로컬 설치 (테스트용)
pip install -e .

# 전체 테스트 (65개)
make test

# 모듈별 테스트
make test-memory       # 메모리 시스템 (10개)
make test-subagents    # SubAgent 상태 전이 (14개)
make test-resilience   # 복원력 (21개)
```

---

## 프로젝트 구조

```
ax_advanced_coding_ai_agent/
├── coding_agent/                   # 메인 패키지
│   ├── config.py                   # 환경변수, 모델 티어 설정
│   ├── models.py                   # 4-Tier 모델 팩토리 (인스턴스 캐시)
│   ├── logging_config.py           # structlog + 파일 로깅
│   ├── core/                       # 에이전트 루프
│   │   ├── state.py                # AgentState TypedDict
│   │   ├── loop.py                 # LangGraph 메인 루프 (타이밍 계측 포함)
│   │   ├── orchestrator.py         # 태스크 라우팅
│   │   ├── tool_adapter.py         # 오픈소스 모델 tool calling 어댑터
│   │   └── tool_call_utils.py      # JSON 복구, 고아 정리, DashScope 직렬화
│   ├── memory/                     # 3계층 장기 메모리
│   │   ├── schema.py               # MemoryRecord (user/project/domain)
│   │   ├── store.py                # SQLite + FTS5 (upsert, search, delete)
│   │   ├── extractor.py            # LLM 자동 추출 (사용자 입력 기반)
│   │   └── middleware.py           # 시스템 프롬프트 주입 (세션 캐시)
│   ├── subagents/                  # 동적 SubAgent 수명주기
│   │   ├── models.py               # 8상태 FSM, SubAgentInstance
│   │   ├── registry.py             # 인스턴스 추적, 이벤트 로그
│   │   ├── factory.py              # 역할 템플릿 + 키워드/LLM 동적 분류
│   │   └── manager.py              # spawn, retry, 컨텍스트 격리, 조기 종료
│   ├── resilience/                 # Agentic Loop 복원력
│   │   ├── watchdog.py             # asyncio timeout
│   │   ├── retry_policy.py         # 에러 분류 + 7가지 장애 유형 정책
│   │   ├── progress_guard.py       # 무한 루프 감지
│   │   ├── safe_stop.py            # 안전 중단 조건
│   │   └── error_handler.py        # 통합 에러 처리 (retry/fallback/abort)
│   ├── tools/                      # 도구 시스템
│   │   ├── file_ops.py             # 파일 CRUD (결과 캐싱 포함)
│   │   ├── shell.py                # 셸 실행
│   │   └── task_tool.py            # SubAgent 위임 도구
│   ├── cli/                        # 대화형 CLI
│   │   ├── app.py                  # REPL + 스트리밍 도구 호출 표시
│   │   └── display.py              # Rich 출력 포매팅
│   └── utils/                      # 유틸리티
│       └── langfuse_trace_exporter.py  # Langfuse 트레이스 추출
├── tests/                          # 테스트 (65개)
│   ├── test_memory.py              # 메모리 시스템 (10개)
│   ├── test_subagents.py           # SubAgent 상태 전이 (14개)
│   ├── test_resilience.py          # 복원력 (21개)
│   └── test_performance.py         # 성능 최적화 (18개) — 캐시, 병렬, 조기종료
├── Dockerfile                      # 에이전트 Docker 이미지
├── docker-compose.yml              # 풀스택 (Agent + LiteLLM + PostgreSQL)
├── litellm_config.yaml             # LiteLLM Proxy 모델 라우팅
├── ax-agent.sh                     # 실행 스크립트
├── entrypoint.sh                   # Docker 엔트리포인트 (UID/GID 매핑)
├── pyproject.toml                  # Python 의존성
├── Makefile                        # 빌드/테스트/배포 명령
├── AGENTS.md                       # AI 에이전트 규칙 문서
├── EVIDENCE.md                     # 요구사항 증빙 문서
└── .env.example                    # 환경변수 템플릿
```

---

## 참고 프로젝트 및 차용 패턴

| 프로젝트 | 차용 패턴 |
|----------|----------|
| **DeepAgents** | "call once, return control" 원칙, `task()` 위임 도구, `<agent_memory>` 주입 |
| **Claude Code** | Coordinator 패턴 (Orchestrator가 코드 안 씀), 컨텍스트 격리, 3계층 메모리 |
| **Codex** | 상태 머신 설계, AgentRegistry 패턴 |

---

## 슬래시 커맨드

| 커맨드 | 설명 |
|--------|------|
| `/help` | 도움말 |
| `/memory` | 저장된 메모리 목록 |
| `/memory add <layer> <key> <content>` | 메모리 수동 추가 |
| `/memory delete <key>` | 메모리 삭제 |
| `/agents` | SubAgent 인스턴스 목록 |
| `/events` | SubAgent 이벤트 로그 |
| `/status` | 시스템 상태 |
| `/resume` | 중단된 작업 이어서 진행 |
| `/exit` | 종료 |

---

## 자기 점검

| # | 질문 | 답변 | 근거 |
|---|------|------|------|
| 1 | 장기 메모리 설계가 있는가? | **예** | user/project/domain 3계층 SQLite+FTS5 |
| 2 | 도메인 지식이 이후 작업에서 재사용되는가? | **예** | inject()에서 FTS5 검색 후 XML 주입 |
| 3 | SubAgent가 런타임 생성되는가? | **예** | 키워드+LLM 분류 후 역할/도구/모델 결정 |
| 4 | SubAgent 상태 전이가 기록되는가? | **예** | SubAgentEvent 이벤트 로그 |
| 5 | SubAgent 실패/blocked 처리가 있는가? | **예** | retry_count + BLOCKED/FAILED 상태 전이 |
| 6 | LLM 실패 시 retry/fallback/safe stop이 있는가? | **예** | 7가지 장애 유형별 정책 |
| 7 | 안전하게 멈추는 기준이 있는가? | **예** | SafeStop + 연속 에러 3회 + max_iterations |
| 8 | DeepAgents 동등 역량 설명 가능한가? | **예** | EVIDENCE.md 매핑 테이블 |
| 9 | 오픈소스 모델인가? | **예** | Qwen 계열 4종 (DashScope 직접 호출) |
| 10 | 실제 E2E 검증이 있는가? | **예** | 6차 실행, 16 SubAgent, 66 파일 생성, `EVIDENCE.md` 섹션 8 |
| 11 | Orchestrator와 SubAgent 경계가 명확한가? | **예** | Orchestrator에 쓰기 도구 없음, `loop.py` L144 참조 |
| 12 | 자체 검증 사이클이 동작하는가? | **예** | reviewer → fixer → reviewer → verifier → planner(FINAL_REPORT) 파이프라인 |
