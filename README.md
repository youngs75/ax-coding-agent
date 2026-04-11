# AX Coding Agent

3계층 장기 메모리, 동적 SubAgent 수명주기, Agentic Loop 복원력을 갖춘 AI Coding Agent Harness.

## 핵심 아키텍처

```
┌─────────────────────────────────────────────────┐
│                   CLI (REPL)                     │
├─────────────────────────────────────────────────┤
│              Main Agent Loop (LangGraph)         │
│                                                  │
│  inject_memory → agent → tools → extract_memory │
│       ↑              ↓           ↓               │
│       │        check_progress  handle_error      │
│       │              ↓           ↓               │
│       └──────── continue ←── safe_stop → END    │
├─────────────────────────────────────────────────┤
│  Memory System  │  SubAgent System │ Resilience  │
│  ─────────────  │  ─────────────── │ ──────────  │
│  user/profile   │  Dynamic Factory │ Watchdog    │
│  project/context│  State Machine   │ RetryPolicy │
│  domain/knowl.  │  Registry+Events │ ProgressGrd │
│  SQLite+FTS5    │  LangGraph sub   │ SafeStop    │
│  Auto-Extract   │  8-state FSM     │ ErrorHandler│
├─────────────────────────────────────────────────┤
│              Tools (8개)                         │
│  read_file │ write_file │ edit_file │ glob_files │
│  grep │ execute │ task (SubAgent) │ memory      │
├─────────────────────────────────────────────────┤
│         Models (4-Tier via OpenRouter/DashScope) │
│  REASONING │ STRONG │ DEFAULT │ FAST             │
│  (Qwen 오픈소스 모델)                             │
└─────────────────────────────────────────────────┘
```

## 요구사항 매핑

### 1. 장기 메모리 (`coding_agent/memory/`)

| 요구사항 | 구현 위치 | DeepAgents 대응 |
|----------|----------|----------------|
| 3계층 분리 (user/project/domain) | `schema.py` MemoryRecord.layer | `MemoryMiddleware` |
| 저장/조회/갱신/정정 | `store.py` MemoryStore (SQLite+FTS5) | AGENTS.md 파일 |
| 자동 추출 | `extractor.py` MemoryExtractor (LLM) | 수동 |
| 시스템 프롬프트 주입 | `middleware.py` inject() | `wrap_model_call()` |
| 이후 작업에서 재사용 | inject → `<agent_memory>` XML 블록 | `<agent_memory>` 태그 |

**충족 시나리오:**
- "타입 힌트 강제" 입력 → `project/context`에 저장 → 이후 코드 생성에서 자동 반영
- "Silver 등급 환불 수수료 0%" 입력 → `domain/knowledge`에 저장 → 결제 로직 생성 시 참조

### 2. 동적 SubAgent (`coding_agent/subagents/`)

| 요구사항 | 구현 위치 | DeepAgents 대응 |
|----------|----------|----------------|
| 런타임 동적 생성 | `factory.py` create_for_task() (LLM 분석) | `SubAgentMiddleware` |
| 8상태 FSM | `models.py` SubAgentStatus + VALID_TRANSITIONS | 3가지 SubAgent 타입 |
| 메타데이터 추적 | `models.py` SubAgentInstance | SubAgent TypedDict |
| 상태 전이 로그 | `registry.py` SubAgentEvent | - |
| 실패/재시도 처리 | `manager.py` _execute_with_retries() | - |
| 컨텍스트 격리 | manager: task_summary만 전달 | _EXCLUDED_STATE_KEYS |
| 정리 정책 | registry.cleanup_completed() | - |

**상태 전이:**
```
CREATED → ASSIGNED → RUNNING → COMPLETED → DESTROYED
                       ↓ ↑         ↓
                    BLOCKED    FAILED → ASSIGNED (retry)
                       ↓              → DESTROYED
                    CANCELLED → DESTROYED
```

### 3. Agentic Loop 복원력 (`coding_agent/resilience/`)

| 장애 유형 | 감지 | 재시도 | 폴백 | 구현 위치 |
|----------|------|--------|------|----------|
| 모델 무응답 | asyncio.TimeoutError | 2회 | FAST 모델 | `watchdog.py` |
| 무진전 루프 | 3회 동일 액션 | 0 | 전략 변경 | `progress_guard.py` |
| 잘못된 tool call | 스키마 검증 | 1회 | - | `retry_policy.py` |
| SubAgent 실패 | FAILED 전이 | 1회 | 다른 역할 | `error_handler.py` |
| 외부 API 오류 | 4xx/5xx | 3회 | - | `retry_policy.py` |
| 모델 폴백 | 컨텍스트 초과 | 0 | 하위 티어 | `error_handler.py` |
| 안전 중단 | 위험 작업 | 0 | 없음 | `safe_stop.py` |

## 모델 정책

**오픈소스 모델 사용** (OpenRouter/DashScope 경유):

| 티어 | 모델 | 용도 |
|------|------|------|
| REASONING | qwen3-max | 계획, 아키텍처 설계 |
| STRONG | qwen3-coder-plus | 코드 생성, 도구 호출 |
| DEFAULT | qwen3-coder-next | 분석, 검증 |
| FAST | qwen3.5-flash | 파싱, 분류, 메모리 추출 |

**모델 선택 이유:**
- Qwen 계열은 tool calling 지원이 안정적
- 150B 미만으로 현실적 비용
- OpenRouter를 통해 통합 API 접근

**폴백 전략:**
REASONING → STRONG → DEFAULT → FAST 순서로 자동 전환

## 빠른 시작

```bash
# 1. 설치
pip install -e .

# 2. 환경변수 설정
cp .env.example .env
# .env 파일에 API 키 입력

# 3. 실행
ax-agent
# 또는
python -m coding_agent.cli.app

# 4. 테스트
make test
```

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
| `/exit` | 종료 |

## 프로젝트 구조

```
coding_agent/
├── config.py              # 환경변수, 모델 티어 설정
├── models.py              # LiteLLM 4-Tier 모델 팩토리
├── core/
│   ├── state.py           # AgentState TypedDict
│   ├── loop.py            # LangGraph 메인 루프
│   └── orchestrator.py    # 태스크 라우팅
├── memory/
│   ├── schema.py          # MemoryRecord
│   ├── store.py           # SQLite + FTS5
│   ├── extractor.py       # LLM 자동 추출
│   └── middleware.py      # 시스템 프롬프트 주입
├── subagents/
│   ├── models.py          # 상태 Enum, 메타데이터
│   ├── registry.py        # 인스턴스 추적, 이벤트 로그
│   ├── factory.py         # LLM 동적 생성
│   └── manager.py         # 수명주기 관리
├── resilience/
│   ├── watchdog.py        # asyncio timeout
│   ├── retry_policy.py    # 에러 분류 + 백오프
│   ├── progress_guard.py  # 무한 루프 감지
│   ├── safe_stop.py       # 안전 중단
│   └── error_handler.py   # 통합 에러 처리
├── tools/
│   ├── file_ops.py        # 파일 CRUD
│   ├── shell.py           # 셸 실행
│   └── task_tool.py       # SubAgent 위임
└── cli/
    ├── app.py             # 대화형 REPL
    └── display.py         # Rich 출력
```

## 참고 프로젝트

| 프로젝트 | 차용 패턴 |
|----------|----------|
| DeepAgents | 미들웨어 패턴, `task()` 도구, `<agent_memory>` 주입 |
| Claude Code | 3계층 메모리, forked agent 격리, auto-extract |
| Codex | 상태 머신, AgentRegistry, Mailbox 패턴 |

## 자기 점검

1. 장기 메모리 설계가 있는가? **예** — user/project/domain 3계층 SQLite+FTS5
2. 도메인 지식이 이후 작업에서 재사용되는가? **예** — inject()에서 FTS5 검색 후 주입
3. SubAgent가 런타임 생성되는가? **예** — LLM이 역할/도구/모델 결정
4. SubAgent 상태 전이가 기록되는가? **예** — SubAgentEvent 이벤트 로그
5. SubAgent 실패/blocked 처리가 있는가? **예** — retry_count + BLOCKED 상태
6. LLM 실패 시 retry/fallback/safe stop이 있는가? **예** — 7가지 장애 유형 모두 처리
7. 안전하게 멈추는 기준이 있는가? **예** — SafeStop 조건 체크
8. 오픈소스 모델인가? **예** — Qwen 계열 (OpenRouter 경유)
