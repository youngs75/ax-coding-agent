# Repository Guidelines

## 프로젝트 개요
AX Coding Agent — 3계층 장기 메모리, 동적 SubAgent 수명주기, Agentic Loop 복원력을 갖춘 AI Coding Agent Harness.
오픈소스 모델(Qwen, GLM)을 활용하며, LiteLLM Gateway + Langfuse 관측성을 통한 LLM 운영 체계를 포함한다.

## 프로젝트 구조

```
ax_advanced_coding_ai_agent/
├── coding_agent/                   # 메인 패키지
│   ├── core/                       # 에이전트 루프, 상태, 오케스트레이터, 도구 어댑터
│   ├── memory/                     # 3계층 장기 메모리 (user/project/domain)
│   ├── subagents/                  # 동적 SubAgent 수명주기 관리
│   ├── resilience/                 # Agentic Loop 복원력 (watchdog, retry, safe stop)
│   ├── tools/                      # 도구 시스템 (파일, 셸, SubAgent 위임)
│   ├── cli/                        # 대화형 CLI (Rich + prompt-toolkit)
│   └── utils/                      # 유틸리티 (Langfuse 트레이스 추출 등)
├── tests/                          # 유닛 테스트 (47개)
├── memory_store/                   # SQLite 메모리 DB (런타임 생성)
├── docker-compose.yml              # 풀스택 배포 (Agent + LiteLLM + Langfuse)
├── Dockerfile                      # 에이전트 Docker 이미지
├── litellm_config.yaml             # LiteLLM Proxy 모델 라우팅 설정
├── ax-agent.sh                     # 실행 스크립트
├── pyproject.toml                  # Python 의존성
├── AGENTS.md                       # 이 파일 — AI와 기여자가 따를 규칙 문서
└── README.md                       # 프로젝트 소개 및 요구사항 매핑
```

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
make test-subagents      # SubAgent 상태 전이
make test-resilience     # 복원력 (timeout/retry/safe stop)
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

### 2. 동적 SubAgent (`coding_agent/subagents/`)
상태 머신: CREATED → ASSIGNED → RUNNING → COMPLETED → DESTROYED
LLM이 역할/도구/모델을 런타임에 결정하여 SubAgent를 동적 생성.

### 3. Agentic Loop 복원력 (`coding_agent/resilience/`)
7가지 장애 유형에 대한 감지/재시도/폴백/안전 중단 정책.

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
