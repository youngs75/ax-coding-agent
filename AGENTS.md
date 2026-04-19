# Repository Guidelines

## 프로젝트 개요
AX Coding Agent — 3계층 장기 메모리, 동적 SubAgent 수명주기, Agentic Loop 복원력을 갖춘 AI Coding Agent Harness.
오픈소스 모델(Qwen, GLM)을 활용하며, LiteLLM Gateway + Langfuse 관측성을 통한 LLM 운영 체계를 포함한다.

**포지셔닝** (2026-04-19~): 과제 제출(2026-04-12) 이후 harness 5책임(Safety/Detection/Clarity/Context/Observation)을
`minyoung-mah` 라이브러리로 추출했고, 이 repo 는 해당 라이브러리의 세 번째 소비자이다. Orchestrator / ProgressGuard /
SqliteMemoryStore / SubAgentRole / ToolAdapter / ResiliencePolicy 등 공통 추상은 라이브러리가 소유하며, 이 repo 는
coding-domain 특화 부분만 담당한다:
  - 6개 SubAgent role prompts (`coding_agent/subagents/roles.py`)
  - file/shell ToolAdapter 어댑터 (`coding_agent/tools/adapters.py`) + HITL ask adapter (`coding_agent/tools/ask_adapter.py`)
  - LangGraph 최상위 ReAct 드라이버 (`coding_agent/core/loop.py`)
  - Rich + prompt-toolkit 대화형 CLI (`coding_agent/cli/`)
  - 3-tier memory extractor + middleware (`coding_agent/memory/`)
  - Langfuse span forwarder (`coding_agent/observability/langfuse_observer.py`)

## 프로젝트 구조

```
ax_advanced_coding_ai_agent/
├── coding_agent/                   # 메인 패키지 (application 레이어)
│   ├── core/                       # LangGraph 최상위 ReAct 드라이버 + agent state
│   ├── memory/                     # 3계층 메모리 extractor/middleware
│   │                               #   (store 는 minyoung_mah.SqliteMemoryStore)
│   ├── subagents/                  # roles.py (6 SubAgentRole) + orchestrator_factory
│   │                               #   + user_decisions + classifier
│   │                               #   (manager/registry/factory 는 minyoung_mah 가 소유)
│   ├── resilience_compat.py        # Watchdog / SafeStop / tier-fallback ErrorHandler
│   │                               #   (ProgressGuard 는 minyoung_mah 가 소유)
│   ├── tools/                      # file/shell StructuredTool + ToolAdapter 래퍼
│   │                               #   + ask/todo/task 도구
│   ├── observability/              # Observer 구현 (StructlogObserver + Langfuse span)
│   ├── cli/                        # 대화형 CLI (Rich + prompt-toolkit)
│   └── utils/                      # Langfuse trace 익스포터
├── tests/                          # 유닛 테스트 (187개, Phase 8 기준)
├── memory_store/                   # SQLite 메모리 DB (런타임 생성, ax.v2.db)
├── docker-compose.yml              # 풀스택 배포 (Agent + LiteLLM + Langfuse)
├── Dockerfile                      # 에이전트 Docker 이미지
├── litellm_config.yaml             # LiteLLM Proxy 모델 라우팅 설정
├── ax-agent.sh                     # 실행 스크립트
├── pyproject.toml                  # Python 의존성 (minyoung-mah editable 포함)
├── AGENTS.md                       # 이 파일 — AI와 기여자가 따를 규칙 문서
└── README.md                       # 프로젝트 소개 및 요구사항 매핑
```

라이브러리가 소유하는 부분 (수정은 `../minyoung-mah` 에서): Orchestrator, SubAgentRole protocol,
ToolAdapter protocol, ProgressGuard, SqliteMemoryStore, TieredModelRouter, HITLChannel,
StructlogObserver, CompositeObserver, ResiliencePolicy, default_resilience.

규칙이 여러 곳에 흩어져 있어도 기준 문서는 항상 `AGENTS.md`로 통일합니다.

## 커뮤니케이션 규칙
사용자와의 모든 소통은 항상 한국어로 진행합니다. 코드 주석은 영어를 기본으로 하되, 사용자 facing 메시지는 한국어를 사용합니다.

## 세션 파일 명명 규칙
세션 파일은 `.ai/sessions/session-YYYY-MM-DD-NNNN.md` 형식을 사용합니다.

- `YYYY-MM-DD`: 세션 당일 날짜
- `NNNN`: 같은 날짜 내 순번 (`0001`부터 시작)
- 같은 날짜 파일이 있으면 가장 큰 번호에 `+1`을 적용합니다.

## Resume 규칙
사용자가 `resume` 또는 `이어서`라고 요청하면 가장 최근 세션 파일을 찾아 이어서 작업합니다.

- `.ai/sessions/`에서 명명 규칙에 맞는 파일만 후보로 봅니다.
- 가장 최신 날짜를 우선 선택하고, 같은 날짜면 가장 큰 순번을 선택합니다.
- 초기 컨텍스트에 파일이 없어 보여도 실제 파일 시스템을 다시 확인합니다.
- 세션 파일 조회 또는 읽기가 샌드박스 제한으로 실패하면, `.ai/sessions/` 확인과 대상 파일 읽기에 필요한 최소 범위에서 권한 상승을 요청한 뒤 즉시 재시도합니다.
- 권한 상승이 필요한 이유는 세션 복구를 위한 실제 파일 시스템 확인임을 사용자에게 짧게 알립니다.
- 선택한 세션 파일은 전체를 읽습니다.
- 사용자에게 이전 작업 내용과 다음 할 일을 한국어로 간단히 브리핑합니다.

## Handoff 규칙
새 세션 파일은 사용자가 명시적으로 종료를 요청한 경우에만 생성합니다. 허용 트리거 예시는 `handoff`, `정리해줘`, `세션 저장`, `종료하자`, `세션 종료`입니다.

- 저장 위치는 항상 `.ai/sessions/`입니다.
- 기존 `session-*.md` 파일은 절대 수정하지 않습니다.
- 자동 저장이나 단계별 저장은 하지 않습니다.
- 새 파일에는 프로젝트 개요, 최근 작업 내역, 현재 상태, 다음 단계, 중요 참고사항을 포함합니다.
- 저장 후 사용자에게 생성된 파일 경로를 알립니다.

## 개발 및 검증 규칙

### 환경 설정
```bash
# 로컬 설치
pip install -e .

# Docker 실행 (권장)
./ax-agent.sh [workspace_path]

# 디버그 모드 (콘솔에 전체 로그)
AX_DEBUG=1 ./ax-agent.sh
```

### 테스트 실행
```bash
make test                # 전체 테스트
make test-memory         # 메모리 시스템
make test-roles          # SubAgentRole + UserDecisionsLog + ask adapter
make test-resilience     # Watchdog/SafeStop/ErrorHandler (ProgressGuard 는 라이브러리 테스트)
```

### Docker 배포
```bash
# 에이전트 단독 실행
./ax-agent.sh

# 풀스택 (Agent + LiteLLM Gateway + Langfuse)
make docker-up
docker compose run --rm agent

# 종료
make docker-down
```

## 디렉토리별 AGENTS.md 관리 원칙
모든 주요 디렉토리에는 `AGENTS.md` 파일을 유지합니다. AI 도구가 디렉토리 구조를 빠르게 파악하도록 돕습니다.

### 필수 포함 섹션
- **Purpose** — 이 디렉토리가 무엇을 하는지 1-2문장
- **Key Files** — 주요 파일과 역할 (테이블)
- **For AI Agents** — 이 디렉토리에서 작업할 때 알아야 할 규칙/패턴

### 관리 규칙
- 새 디렉토리를 만들면 `AGENTS.md`도 함께 생성합니다.
- `<!-- Parent: ../AGENTS.md -->` 주석으로 상위 문서를 참조합니다.

## 핵심 아키텍처: 3축

### 1. 장기 메모리 (`coding_agent/memory/`)
| 계층 | 저장 내용 | 저장소 |
|------|----------|--------|
| `user` | 선호/습관/피드백 | SQLite + FTS5 |
| `project` | 아키텍처/규칙/결정 | SQLite + FTS5 |
| `domain` | 비즈니스 용어/규칙 | SQLite + FTS5 |

→ 저장소는 `minyoung_mah.SqliteMemoryStore` 가 소유 (tier/scope 스키마).
  extractor + middleware + 3-layer semantic 은 이 repo 에 남음.

### 2. SubAgent 체계 (`coding_agent/subagents/`)
`minyoung_mah.Orchestrator` 가 역할 invocation 전체를 담당한다. 이 repo 에는
역할 정의(6개 `SubAgentRole` 구현, `roles.py`) + Orchestrator 빌더
(`orchestrator_factory.py`) + 분류기(`classifier.py`) + user decisions 누적기만 남는다.
기존 CREATED→ASSIGNED→RUNNING→COMPLETED 상태 머신은 라이브러리가 소유
(`minyoung_mah.RoleStatus`).

### 3. Agentic Loop 복원력
`minyoung_mah.ProgressGuard` + `default_resilience` 가 핵심 감지/타임아웃 로직 소유.
ax-specific tier fallback + dangerous-path SafeStop + Watchdog 은 `resilience_compat.py`
에 남아 최상위 LangGraph `handle_error` 노드가 계속 사용 (plan §결정 2).

## 4-Tier 모델 체계
| 티어 | 용도 | 환경변수 |
|------|------|----------|
| **REASONING** | 계획/아키텍처 설계 | `REASONING_MODEL` |
| **STRONG** | 코드 생성/도구 호출 | `STRONG_MODEL` |
| **DEFAULT** | 분석/검증 | `DEFAULT_MODEL` |
| **FAST** | 파싱/분류/메모리 추출 | `FAST_MODEL` |

`.env`에서 모델 오버라이드 가능. LiteLLM Proxy 경유 시 Langfuse 자동 트레이싱.

## 주요 기술 스택
- **LangGraph** — 상태 그래프 기반 에이전트 루프
- **LangChain** — LLM 추상화
- **LiteLLM** — 멀티 프로바이더 LLM Gateway
- **SQLite + FTS5** — 장기 메모리 저장소
- **Rich + prompt-toolkit** — 대화형 CLI
- **Docker Compose** — 배포
- **Langfuse** — 관측성

## 커밋 규칙
Conventional Commits: `feat:`, `fix:`, `docs:` 등.
`.env`, `.db`, `.ax-agent/`, `.claude/`는 커밋하지 않습니다.
