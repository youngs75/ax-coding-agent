# 작업지시서 — apt-web routing v2 (`/coding` 분리 + 산출물 다운로드 UI)

> **대상**: apt-web 담당 Claude IDE (별도 터미널 Claude Code 세션,
>   cwd `C:/projects/apt-web`)
> **요청자**: ax-coding-agent VDI Claude
> **이전**: v1 작업지시서 (`apt-web-integration-v1-instructions.md`) 가 적용
>   완료된 상태 — `chat_coding.html` / `chat_legal.html` 분리 + 8 SSE event
>   매핑 + HITL modal + todo panel 등.
> **목적**: ① top-level URL 분리(`/chat/coding` → `/coding`, `/chat/legal`
>   → `/legal`) ② ax 측 신규 산출물 다운로드 endpoint 와 연동.

---

## 0. 컨텍스트

ax-coding-agent 측에서 곧 다음 endpoint 가 추가됩니다 (별도 commit, GitLab
배포 후 적용):

```
GET /artifacts/__bundle.zip                — workspace 전체 zip 스트리밍
GET /artifacts/{path:path}                 — workspace 내 개별 파일
```

apt-web 은 이걸 chat_coding.html 의 "다운로드" UI 로 노출하고, 동시에
top-level URL 을 정리해서 페이지 분리를 명확히 하고 싶습니다.

한국어 소통, 코드 주석은 영문+한글 병기 (사용자 글로벌 정책). conventional
commits, **push 는 사용자가 직접** (이 세션에서는 commit 까지만).

---

## 1. 사전 확인

```bash
pwd                                # C:/projects/apt-web
git status
git log --oneline -10              # v1 작업지시서 적용 commit 확인
git branch --show-current          # main 또는 feat/ 브랜치
```

새 브랜치:
```bash
git checkout -b feat/routing-v2
```

---

## 2. 변경 미리보기 — URL map

| 항목 | 현재 (v1) | 변경 후 (v2) |
|---|---|---|
| 코딩 chat HTML | `GET /chat/coding` | **`GET /coding`** |
| 법률 chat HTML | `GET /chat/legal`  | **`GET /legal`** |
| 코딩 A2A relay | `POST /chat/a2a/coding/{tasks/send,stream,respond}` | **`POST /coding/a2a/{tasks/send,stream,respond}`** |
| 법률 A2A relay | `POST /chat/a2a/legal/{tasks/send,stream,respond}`  | **`POST /legal/a2a/{tasks/send,stream,respond}`** |
| 코딩 agent card | `GET /chat/a2a/coding/.well-known/agent.json` | **`GET /coding/a2a/.well-known/agent.json`** |
| 코딩 산출물 (신규) | — | **`GET /coding/a2a/artifacts/__bundle.zip`** + **`GET /coding/a2a/artifacts/{path:path}`** |
| 법률 산출물 | — | (해당 없음 — apt-legal 미구현) |

### Backward-compat 결정

- `/chat/coding`, `/chat/legal`, `/chat/a2a/{agent_id}/*` 의 기존 라우트는
  **유지하되 deprecated** 로 두고 docstring 에 `# DEPRECATED — v2 에서
  /{agent_id}/* 로 이동` 명시. 외부 의존자(예: 북마크) 영향 최소화. 6주 후
  제거 예정 한 줄 주석.

---

## 3. 작업

### 작업 1 — `src/apt_web/chat/router.py` (또는 신규 라우터 분리)

현재 `chat/router.py` 가 `/chat/...` prefix 를 모두 모음. v2 에서는:

**옵션 A (권장)** — 라우터 *재배치*. 같은 router.py 안에서 prefix 를 빈
문자열로 두고 각 경로 명시:

```python
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

router = APIRouter()  # prefix 없음

# ── HTML 페이지 ─────────────────────────────────────────────────────────
@router.get("/coding", response_class=HTMLResponse)
async def page_coding() -> FileResponse:
    return FileResponse("src/apt_web/static/chat_coding.html")

@router.get("/legal", response_class=HTMLResponse)
async def page_legal() -> FileResponse:
    return FileResponse("src/apt_web/static/chat_legal.html")

# ── DEPRECATED — v1 호환 (6주 후 제거) ─────────────────────────────────
@router.get("/chat/coding", response_class=HTMLResponse)
async def page_coding_legacy() -> FileResponse:
    return FileResponse("src/apt_web/static/chat_coding.html")

@router.get("/chat/legal", response_class=HTMLResponse)
async def page_legal_legacy() -> FileResponse:
    return FileResponse("src/apt_web/static/chat_legal.html")
```

**옵션 B** — 새 라우터 파일 (`coding/router.py`, `legal/router.py`) 를 만들고
`main.py` 에서 prefix 로 등록. 깔끔하지만 변경 범위 큼.

권장: 옵션 A — *코드 변경 최소* + 같은 파일 안에서 정리.

### 작업 2 — A2A relay endpoint 재작성 (`src/apt_web/chat/router.py`)

다음 패턴으로 4개 함수 (`tasks/send`, `stream`, `respond`, agent card)
+ 산출물 2개 (zip, path file). agent_id 별로 *팩토리 함수* 또는 *명시
경로* 둘 다 가능. 명시 경로가 단순:

```python
# ── /coding 라우트 (ax-coding-agent 로 relay) ──────────────────────────
@router.post("/coding/a2a/tasks/send")
async def coding_tasks_send(request: Request) -> JSONResponse:
    return await _proxy_a2a_send(request, agent_id="coding")

@router.post("/coding/a2a/stream")
async def coding_stream(request: Request) -> StreamingResponse:
    return await _proxy_a2a_stream(request, agent_id="coding")

@router.post("/coding/a2a/respond")
async def coding_respond(request: Request) -> JSONResponse:
    return await _proxy_a2a_respond(request, agent_id="coding")

@router.get("/coding/a2a/.well-known/agent.json")
async def coding_agent_card(request: Request) -> JSONResponse:
    return await _proxy_agent_card(request, agent_id="coding")

# ── 산출물 다운로드 (신규) ─────────────────────────────────────────────
@router.get("/coding/a2a/artifacts/{path:path}")
async def coding_artifacts(path: str) -> StreamingResponse:
    """Forward GET /artifacts/{path} including __bundle.zip to ax-coding-agent.

    ax 측 endpoint:
    - /artifacts/__bundle.zip  — workspace 전체 zip
    - /artifacts/{path}        — 개별 파일
    """
    return await _proxy_artifacts(path, agent_id="coding")


# ── /legal 라우트 (apt-legal-agent 로 relay) ────────────────────────────
@router.post("/legal/a2a/tasks/send")
async def legal_tasks_send(request: Request) -> JSONResponse:
    return await _proxy_a2a_send(request, agent_id="legal")

@router.post("/legal/a2a/stream")
async def legal_stream(request: Request) -> StreamingResponse:
    return await _proxy_a2a_stream(request, agent_id="legal")

@router.post("/legal/a2a/respond")
async def legal_respond(request: Request) -> JSONResponse:
    return await _proxy_a2a_respond(request, agent_id="legal")

@router.get("/legal/a2a/.well-known/agent.json")
async def legal_agent_card(request: Request) -> JSONResponse:
    return await _proxy_agent_card(request, agent_id="legal")

# (apt-legal 은 산출물 endpoint 미구현 — 추가 안 함)


# ── DEPRECATED — v1 라우트 (6주 후 제거) ───────────────────────────────
@router.post("/chat/a2a/{agent_id}/tasks/send")
async def chat_a2a_tasks_send_legacy(agent_id: str, request: Request):
    return await _proxy_a2a_send(request, agent_id=agent_id)

# ... (기존 4개 endpoint 동일 패턴 deprecated)
```

`_proxy_a2a_send` 등 helper 들은 기존 코드에서 추출. agent_id 에 따라
`settings.agents[agent_id]` 로 base_url 조회.

`_proxy_artifacts`:
```python
async def _proxy_artifacts(path: str, agent_id: str) -> StreamingResponse:
    base_url = settings.agents.get(agent_id)
    if not base_url:
        raise HTTPException(404, f"agent {agent_id!r} not registered")

    async def relay():
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=settings.AGENT_A2A_TIMEOUT_SECONDS,
        ) as client:
            async with client.stream("GET", f"/artifacts/{path}") as upstream:
                # 헤더 forward (Content-Disposition, Content-Length 등)
                # 단 Content-Type 은 zip → application/zip / 일반파일 → octet-stream
                # 응답 streaming 은 raw bytes 그대로
                async for chunk in upstream.aiter_bytes():
                    yield chunk

    # ax 측 응답 헤더에서 Content-Disposition 받아오기 — 1회 HEAD 요청 또는
    # 첫 chunk 전에 헤더 dump. 단순한 접근: GET 하면서 첫 응답 헤더 읽고 흘림.
    return StreamingResponse(
        relay(),
        media_type="application/zip" if path.endswith(".zip") else "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{Path(path).name}"'},
    )
```

(streaming relay 의 정확한 헤더 forward 는 표준 패턴 검색 — `httpx.stream` +
`StreamingResponse` 조합. 단 핵심은 *response body 의 raw byte stream* 을
끊김 없이 전달.)

### 작업 3 — `src/apt_web/static/chat_common.js` fetch URL 변경

기존:
```javascript
fetch(`/chat/a2a/${agentId}/tasks/send`, ...)
fetch(`/chat/a2a/${agentId}/stream`, ...)
fetch(`/chat/a2a/${agentId}/respond`, ...)
```

변경:
```javascript
fetch(`/${agentId}/a2a/tasks/send`, ...)
fetch(`/${agentId}/a2a/stream`, ...)
fetch(`/${agentId}/a2a/respond`, ...)
```

agent_id 가 `coding` 일 때 → `/coding/a2a/...`, `legal` 일 때 →
`/legal/a2a/...` 자동 매핑.

### 작업 4 — `chat_coding.html` 에 산출물 다운로드 UI 추가

todo panel 옆 또는 footer 에 "워크스페이스 다운로드" 버튼:

```html
<!-- 산출물 다운로드 (전체 zip) -->
<a :href="`/${agentId}/a2a/artifacts/__bundle.zip`"
   download
   class="artifact-download-btn">
  📦 워크스페이스 zip 다운로드
</a>
```

또는 conversation 종료 후 `orchestrator.run.end` SSE 받았을 때 자동 표시.
간단한 시작점은 *항상 보임* + 사용자가 클릭 결정.

CSS:
```css
.artifact-download-btn {
  display: inline-block; padding: 8px 14px;
  background: #16a34a; color: white; border-radius: 4px;
  text-decoration: none; font-size: 0.9rem; cursor: pointer;
}
.artifact-download-btn:hover { background: #15803d; }
```

`chat_legal.html` 에는 **추가하지 마세요** — apt-legal 은 산출물 endpoint
미구현.

### 작업 5 — 테스트 업데이트

기존 `tests/test_chat_multiagent.py`, `tests/test_chat_hitl.py` 가
`/chat/a2a/{agent_id}/...` URL 가정. 새 URL 추가:

- `tests/test_routing_v2.py` 신규
  - `GET /coding` → 200 + chat_coding.html 본문
  - `GET /legal` → 200 + chat_legal.html 본문
  - `POST /coding/a2a/tasks/send` → AGENT_CODING_BASE_URL 로 relay
  - `POST /legal/a2a/tasks/send` → AGENT_LEGAL_BASE_URL 로 relay
  - `GET /coding/a2a/artifacts/__bundle.zip` → AGENT_CODING_BASE_URL 의
    `/artifacts/__bundle.zip` 으로 streaming relay (httpx mock)
  - DEPRECATED 라우트 (`/chat/coding`, `/chat/a2a/coding/tasks/send`) 도
    여전히 200 (backward compat)

기존 `test_chat_multiagent.py` 는 *수정 없이* 그대로 통과해야 함
(deprecated 경로 유지로).

```bash
python -m pytest tests/ -v
```

전부 통과 + 신규 테스트도 통과해야 함.

### 작업 6 — `src/apt_web/main.py` 확인

router include 그대로면 OK. 변경 없을 가능성 높음. 다만 `chat/router.py`
가 정말 `/chat` prefix 안 쓰는지 확인 필요. 만약 `app.include_router(chat_router, prefix="/chat")` 같은 패턴이면 prefix 제거 필요.

---

## 4. 커밋 (push 는 사용자가)

논리 단위:
1. `feat(routing): /coding + /legal top-level URL 분리 (v1 /chat/* 호환 유지)`
2. `feat(chat): /{agent_id}/a2a/* relay + DEPRECATED /chat/a2a/{agent_id}/* 호환`
3. `feat(coding): /coding/a2a/artifacts/{path} 다운로드 proxy (ax /artifacts → __bundle.zip 포함)`
4. `feat(chat-ui): chat_common.js fetch URL prefix 갱신 + chat_coding.html 다운로드 버튼`
5. `test: routing v2 — top-level URL + artifacts proxy + backward-compat`

`AGENTS.md` 가 라우팅 규칙 명시하면 그것도 갱신.

---

## 5. 검증

```bash
cd C:/projects/apt-web
python -m pytest tests/ -v               # 전부 통과 + 신규 통과
python -c "from apt_web.main import app; \
    print('paths:', sorted([r.path for r in app.routes if hasattr(r, 'path')]))"
# 기대 출력: /coding, /legal, /coding/a2a/tasks/send, /legal/a2a/stream,
#           /coding/a2a/artifacts/{path:path}, /chat/coding, /chat/a2a/{agent_id}/...
#           등이 모두 등록됨
```

---

## 6. 산출물 보고

- 새 브랜치 이름 (`feat/routing-v2`)
- 변경 파일 list + 각 변경 핵심 (1-2 줄)
- 모든 commit list (subject + sha)
- pytest 결과 (전체 출력)
- 의문/막힌 부분

**중요**: push 는 사용자가 직접. 이 세션에서는 commit 까지만.

---

## 7. 막혔을 때 / 함정

| 증상 | 원인 가정 | 처방 |
|---|---|---|
| `/coding` 이 404 | `chat/router.py` 가 prefix `/chat` 으로 등록됨 | `main.py` 의 `include_router(chat_router)` 에서 prefix 제거 또는 라우트 명시 경로 사용 |
| 산출물 다운로드 502 | ax 측 `/artifacts/__bundle.zip` 미배포 | ax 측이 별도 commit + GitLab push + 사용자 deploy 필요. 사용자 확인 후 진행 |
| chat_coding.html 페이지가 빈 화면 | `/coding` 의 FileResponse 가 정적 파일을 못 찾음 | `pathlib.Path(__file__).parent / "static" / "chat_coding.html"` 절대경로 사용 |
| 기존 chat_legal 사용자 영향 | `/chat/legal` legacy 제거됨 | DEPRECATED 라우트 유지 (6주 그레이스) |

---

## 8. 불변 규칙

- ❌ `git push` 금지 (사용자가 직접)
- ❌ 기존 v1 테스트 깨뜨리지 말 것 (backward-compat 가능한 한 유지)
- ❌ apt-legal-agent 기존 동작 깨뜨리지 말 것
- ✅ 한국어 소통, conventional commits prefix
- ✅ legacy `/chat/*` 라우트는 *deprecated* 상태로 남기고 6주 후 제거 plan
- ✅ chat_legal.html 에는 다운로드 버튼 추가 *안 함*

전체 작업 예상 — 60~90분.
