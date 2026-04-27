# ax-coding-agent 포털 배포 완료 보고서 — 2026-04-27

> **보고자**: 포털 Web IDE Claude Code 세션
> **수신자**: VDI Claude (ax-coding-agent main 작업자)
> **보고 시각**: 2026-04-27T01:30Z

---

## 1. 작업 요약

GitHub `youngs75/ax-coding-agent` main 코드를 포털 GitLab에 동기화하고,
dummy 스텁이었던 A2A 엔드포인트를 실제 AgentLoop 오케스트레이터에 연결하여
포털 EKS에 배포 완료.

---

## 2. 완료된 단계

| # | 단계 | 결과 | 비고 |
|---|---|---|---|
| 1 | 사전 확인 | OK | GitLab origin 확인, root 권한, Debian 13 |
| 2 | GitHub → GitLab pull + push | OK | 67 commit + merge (`1816417`) |
| 3 | venv + 의존성 설치 | OK | Python 3.12 venv, `pip install -e .` 성공 |
| 4 | 단위 테스트 (dummy) | 8/8 PASSED | tests/web/test_a2a_dummy.py |
| 5 | import + agent card sanity | OK | healthz 200, card name/version/endpoints 정상 |
| 6 | uvicorn 로컬 검증 | OK | 포트 8082 (8080은 포털 인증 프록시 점유) |
| 7 | GitLab push (dummy) | OK | `1816417 → 1159525` |
| 8 | `fix(portal)` GitHub sync | OK | `ef7c905` LITELLM defaults + Dockerfile PORT |
| 9 | **AgentLoop 실연결** | OK | dummy → 실제 `AgentLoop.run()` 호출 |
| 10 | `AX_PROJECT_ROOT` env 추가 | OK | config.py — Volume 마운트 대비 |
| 11 | 테스트 업데이트 | 10/10 PASSED | mock AgentLoop 기반, LLM 호출 없음 |
| 12 | GitLab push (실연결) | OK | `d0ae974` |
| 13 | 포털 배포 | **성공** | `Dockerfile.portal` 기반 kaniko 빌드 |
| 14 | 외부 endpoint 검증 | **전 항목 200** | healthz + agent card + tasks/send |

---

## 3. 외부 endpoint 검증 결과

**Endpoint**: `https://portal-serving-evangelist-1-agent2-e4e8d185.samsungsdscoe.com`

```
GET  /healthz              → 200  {"status":"ok","version":"0.1.0"}
GET  /.well-known/agent.json → 200  card.url = 외부 URL (동적 반영 OK)
POST /a2a/tasks/send        → 200  A2A envelope 정상
```

- agent card의 `url` 및 `endpoints` 가 모두 외부 URL로 동적 반영됨
- reverse proxy 헤더 통과 정상 (`x-forwarded-proto` / `host`)

---

## 4. GitLab commit 이력 (GitHub 대비 추가분)

| SHA | 메시지 | 내용 |
|---|---|---|
| `fe4e051` | `docs(recon): portal EKS environment v1` | 환경 정찰 보고서 (GitLab only) |
| `1816417` | `Merge remote-tracking branch 'origin/main'` | GitLab 정찰 커밋 merge |
| `1159525` | `Merge remote-tracking branch 'github/main'` | GitHub fix(portal) 동기화 |
| **`d0ae974`** | **`feat(web): wire AgentLoop into A2A endpoints + AX_PROJECT_ROOT env`** | **핵심 변경** |

**`d0ae974` 변경 파일 (3개)**:
- `coding_agent/config.py` — `AX_PROJECT_ROOT` env로 `_PROJECT_ROOT` 오버라이드
- `coding_agent/web/app.py` — dummy → AgentLoop.run() 실연결
- `tests/web/test_a2a_dummy.py` — mock 기반 테스트 10개

---

## 5. 현재 배포 ENV 설정

| ENV | 값 | 출처 |
|---|---|---|
| `LLM_PROVIDER` | `litellm_portal` | **사용자가 포털 UI에서 수동 주입** (필수) |
| `LITELLM_BASE_URL` | `https://litellm.samsungsdscoe.com` | config.py 하드코딩 default |
| `LITELLM_API_KEY` | (교육용 KEY) | config.py 하드코딩 default |
| `LITELLM_MODEL` | `us.anthropic.claude-sonnet-4-6` | config.py 하드코딩 default |
| `LANGFUSE_*` | (자동) | 포털 `AGENT_OBSERVABILITY_*` → 자동 미러링 |
| `AX_PROJECT_ROOT` | `/data` (선택) | Volume 마운트 시 사용자가 주입 |
| `PORT` | `8080` | Dockerfile.portal default |

---

## 6. 아키텍처 현황

```
[사용자 브라우저]
    ↓ (apt-web chat UI)
[apt-web BFF]
    ↓ POST /a2a/stream (SSE relay)
[ax-coding-agent EKS Pod]  ← 이번 배포 대상
    ├─ FastAPI daemon (uvicorn :8080)
    │   ├─ /healthz
    │   ├─ /.well-known/agent.json
    │   ├─ /a2a/tasks/send  → AgentLoop.run()
    │   ├─ /a2a/stream      → AgentLoop.run() + SSE
    │   └─ /a2a/respond     → HITL interrupt resume
    └─ AgentLoop
        ├─ LangGraph StateGraph (6 SubAgent)
        ├─ minyoung-mah Orchestrator
        ├─ MemoryStore (SQLite)
        └─ LiteLLM → Claude Sonnet 4.6
```

---

## 7. GitHub 역동기화 필요

GitLab에 3개 추가 커밋이 있음 (`fe4e051`, merge 2개, `d0ae974`).
VDI 측에서 GitHub main으로 역동기화 필요:

```bash
cd /path/to/ax-coding-agent
git remote add gitlab https://gitlab.samsungsdscoe.com/74435f2f-2053-4d88-b00e-55e2b2d92bf0/ax-coding-agent.git
git fetch gitlab main
git merge gitlab/main --no-edit
git push origin main
```

핵심 변경은 `d0ae974` 하나 — 나머지는 merge commit과 정찰 문서.

---

## 8. apt-web 반영 필요 사항

`.ai/apt-web-integration-v1-instructions.md` 작업지시서가 이미 작성돼 있으나,
이번 실배포에서 확인된 사항으로 **업데이트/주의할 내용**:

### 8.1 A2A endpoint contract 변경

작업지시서 §2의 endpoint contract는 유효하나, **응답 형식이 달라짐**:

| endpoint | 작업지시서 가정 | 실제 구현 |
|---|---|---|
| `POST /a2a/tasks/send` | `{"status":"received"}` (dummy) | A2A envelope: `{"id":"...", "status":{"state":"completed"}, "artifacts":[...]}` |
| `POST /a2a/stream` | 단일 dummy SSE event | `task.start` → `task.artifact` → `task.status` → `task.end` 순서 |
| `POST /a2a/respond` | `{"status":"received"}` (dummy) | `{"status":"resumed"}` 또는 `{"status":"received","note":"no pending interrupt"}` |

**apt-web 의 SSE 파서가 이 이벤트명을 처리해야 함.**

### 8.2 SSE event 이름 불일치 — 조정 필요

작업지시서 §3 의 SSE event spec:
```
orchestrator.run.start
orchestrator.run.end
orchestrator.role.invoke.start/end
role.tool.call.start/end
orchestrator.todo.change
orchestrator.critic.verdict
input_required
```

현재 `app.py` 의 `_stream_run()` 이 emit하는 이벤트:
```
task.start
task.artifact
task.status
task.end
```

**차이 원인**: `_stream_run()`은 `AgentLoop.run()`을 한 번 호출하고 최종 결과만 SSE로 감싸는 구조. 작업지시서가 기대하는 세밀한 중간 이벤트(`role.invoke`, `todo.change`, `critic.verdict`, `input_required`)는 AgentLoop 내부에서 발행되지만 현재 SSE 스트림으로 전달되지 않음.

**VDI 측 추가 작업 필요**:

1. **AgentLoop에 event callback/queue 추가**: `run()` 실행 중 내부 이벤트(role invoke, tool call, todo change, critic verdict, interrupt)를 외부로 전달하는 메커니즘
2. **`_stream_run()` 에서 중간 이벤트 SSE 발행**: callback/queue에서 꺼내서 SSE frame으로 변환
3. **`input_required` SSE event**: AgentLoop의 interrupt가 발생하면 SSE로 `input_required` event emit → apt-web UI가 HITL modal → `/a2a/respond`로 답변 → 같은 스트림에서 후속 이벤트 재개

이 작업이 없으면 apt-web 의 todo panel, HITL modal, tool output expand가 동작하지 않음.

### 8.3 apt-web config 변경

```python
AGENT_CODING_BASE_URL = "https://portal-serving-evangelist-1-agent2-e4e8d185.samsungsdscoe.com"
```

이 값을 apt-web의 `Settings`에 주입하거나 ENV로 설정.

### 8.4 HITL respond flow 정합성

현재 `app.py`의 `/a2a/respond`는 `_pending_interrupts` dict 기반이지만, `AgentLoop.run()`이 동기 실행이라 interrupt 시점에 SSE 연결을 유지한 채 대기하는 구조가 아직 없음.

**완전한 HITL flow 구현에 필요한 것**:
1. `AgentLoop.run()`에 `ask_user` 콜백 전달
2. 콜백 내부에서 `asyncio.Future` 생성 → `_pending_interrupts`에 등록 → SSE로 `input_required` emit → `await future`
3. `/a2a/respond` 가 해당 future를 resolve
4. `AgentLoop` 가 `Command(resume=answer)`로 그래프 재개

이것은 `app.py` + `AgentLoop` 양쪽 수정이 필요한 작업.

---

## 9. 리스크 / 미해결 항목

| # | 항목 | 심각도 | 설명 |
|---|---|---|---|
| 1 | SSE 중간 이벤트 미전달 | 높음 | apt-web todo/HITL/tool UI가 동작 안 함 |
| 2 | HITL interrupt-resume 미완성 | 높음 | `/a2a/respond` 구조는 있으나 `ask_user` 콜백 미연결 |
| 3 | LiteLLM 허용 모델 한정 | 중간 | sonnet-4-6만 가능, Opus 필요 시 KEY 확장 |
| 4 | Python 3.13/3.12 불일치 | 낮음 | venv로 해결됨, Dockerfile은 3.12-slim |
| 5 | NFS /workspace latency | 낮음 | node_modules 대량 I/O 시 영향 가능 |
| 6 | 포트 8080 충돌 | 해결됨 | Dockerfile.portal PORT=8080, 배포 환경에서는 정상 |

---

## 10. 다음 단계 권장 순서

1. **GitHub 역동기화** — `d0ae974` 등 GitLab 추가분을 GitHub main에 merge
2. **SSE 중간 이벤트 스트리밍** — `AgentLoop` event callback + `_stream_run()` 확장
3. **HITL interrupt-resume 완성** — `ask_user` 콜백 → asyncio.Future → `/a2a/respond`
4. **apt-web 통합** — `.ai/apt-web-integration-v1-instructions.md` 실행 (§8 반영 포함)
5. **E2E 검증** — apt-web UI → ax-coding-agent 실제 코드 생성 작업 완주
