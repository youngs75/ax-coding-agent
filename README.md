# AX Coding Agent

3계층 장기 메모리, 동적 SubAgent 수명주기, Agentic Loop 복원력을 갖춘 AI Coding Agent Harness.

오픈소스 모델(Qwen)을 활용하며, 자체 LiteLLM Gateway + Langfuse 관측성을 통한 LLM 운영 체계를 포함합니다.

---

## Docker 빌드 및 실행 방법

### 사전 요구사항

- Docker + Docker Compose
- API 키: DashScope (Qwen), Langfuse (선택)

### 1단계: 환경변수 설정

```bash
cp .env.example .env
# .env 파일을 열고 API 키를 입력하세요
```

필수 환경변수:
```
DASHSCOPE_API_KEY=<DashScope API 키>
```

### 2단계: Docker 이미지 빌드

```bash
# 에이전트 이미지 빌드
docker build -t ax-coding-agent .
```

### 3단계: LLM Gateway 기동

```bash
# LiteLLM Proxy + PostgreSQL 기동
docker compose up -d litellm-db litellm

# 헬스 체크 (약 30-60초 소요)
curl http://localhost:4001/health/liveliness
```

### 4단계: 에이전트 실행

```bash
# 방법 A: 실행 스크립트 (권장)
./ax-agent.sh [작업할_디렉토리_경로]

# 방법 B: docker compose
docker compose run --rm agent

# 방법 C: 특정 프로젝트 디렉토리에서 작업
WORKSPACE_DIR=/path/to/project docker compose run --rm agent
```

### 5단계: 종료

```bash
docker compose down
```

### 전체 한 줄 실행 (빌드 + 기동 + 실행)

```bash
docker build -t ax-coding-agent . && \
docker compose up -d litellm-db litellm && \
sleep 30 && \
./ax-agent.sh ~/my-project
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
│  SQLite+FTS5    │  5 Role Template │ SafeStop    │
│  Auto-Extract   │  Context Isolate │ ErrorHandler│
├─────────────────────────────────────────────────┤
│              Tools (7개)                         │
│  read_file │ write_file │ edit_file │ glob_files │
│  grep │ execute │ task (SubAgent 위임)           │
├─────────────────────────────────────────────────┤
│     LiteLLM Gateway → DashScope (Qwen 오픈소스) │
│  REASONING │ STRONG │ DEFAULT │ FAST             │
│     + Langfuse 자동 트레이싱                     │
└─────────────────────────────────────────────────┘
```

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

### 오픈소스 모델 사용 (DashScope 직접 호출)

| 티어 | 모델 | 용도 |
|------|------|------|
| REASONING | `qwen3-max` | 계획, 아키텍처 설계, PRD/SPEC 작성 |
| STRONG | `qwen3-coder-next` | 코드 생성, 도구 호출, TDD 구현 |
| DEFAULT | `qwen3.5-plus` | 분석, 검증, 코드 리뷰 |
| FAST | `qwen3.5-flash` | 파싱, 분류, 메모리 추출 |

**모델 선택 이유:**
- Qwen 계열은 tool calling 지원이 안정적
- DashScope 직접 호출로 네트워크 안정성 확보 (OpenRouter 경유 대비)
- 4-Tier 분리로 작업 복잡도에 맞는 비용 최적화
- LiteLLM Proxy 경유로 Langfuse 자동 트레이싱

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
| 9 | 오픈소스 모델인가? | **예** | Qwen 계열 4종 (DashScope 직접) |
