# 작업지시서 — apt-web 에 ax-coding-agent 통합 추가 (v1)

> **대상**: 별도 터미널에서 `cd C:/projects/apt-web && claude` 로 새로 띄워진
>   Claude Code 세션
> **요청자**: ax-coding-agent worktree 의 main Claude (Youngsuk × Claude 페어
>   세션 진행 중)
> **이유**: apt-web 리포에 대한 쓰기가 sub-agent sandbox 정책으로 차단되어,
>   별도 cwd 의 Claude 세션을 통해 작업 위임. terminal Claude 는 그 cwd 가
>   "내부" 라서 자유롭게 쓰기 가능.

---

## 0. 시작 전 컨텍스트 흡수 (5 분)

당신은 **apt-web** (사내 포털 chat UI / FastAPI BFF) 리포에 진입한 Claude
Code 세션입니다. 한국어로 소통하고, 코드 주석은 영문+한글 병기 (사용자 글로벌
정책).

### 0.1 위치 확인

```bash
pwd                               # C:/projects/apt-web 가 떠야 함
git status
git remote -v
git log --oneline -3
```

cwd 가 `apt-web` 이 아니면 즉시 보고하고 멈추세요.

### 0.2 필수 읽기

작업 시작 전 다음 파일을 모두 읽으세요 (전체):

1. `AGENTS.md` — 프로젝트 규칙 (commit 스타일, 한국어 정책, env 표 등)
2. `src/apt_web/main.py` — FastAPI app, lifespan, 라우터 등록
3. `src/apt_web/config.py` — Settings 클래스 (확장 대상)
4. `src/apt_web/chat/router.py` — 현재 chat 라우터 (대규모 수정 대상)
5. `src/apt_web/static/chat.html` — Vue 3 single-file UI (970줄 정도, UI
   확장 대상)
6. `src/apt_web/a2a/router.py`, `src/apt_web/a2a/agent_card.py` — dummy A2A
   패턴 참조용
7. `tests/test_chat.py`, `tests/test_a2a_dummy.py` — 기존 테스트 패턴 흡수
8. `pyproject.toml` — 의존성·pytest 설정

### 0.3 새 브랜치

```bash
git checkout -b feat/ax-coding-agent-integration
```

이 브랜치에서 작업하세요. **push 는 절대 하지 마세요** — 사용자가 검토 후
직접 push.

---

## 1. 미션 — apt-web 에 두 번째 agent 추가

### 1.1 배경

현재 apt-web 은 apt-legal-agent 1개만 relay 합니다. ax-coding-agent
(LangGraph 기반 multi-agent coding harness) 를 두 번째 agent 로 추가해야
합니다. ax 는 apt-legal 과 다음 두 가지가 본질적으로 다릅니다:

1. **HITL `ask_user_question`** — 작업 도중 사용자에게 1~4 개 다중선택
   modal 을 띄워 답을 받음. SSE 흐름을 일시정지 → 사용자 답을 별도
   endpoint 로 ax 에 전달 → ax 가 LangGraph `Command(resume=...)` 로 그래프
   재개.
2. **풍부한 진행 이벤트** — `orchestrator.todo.change`,
   `role.tool.call.start/end`, `orchestrator.critic.verdict` 등 — apt-legal
   에는 없거나 약한 이벤트. chat UI 가 이걸 시각화해야 함.

### 1.2 첫 MVP 범위 (이번 작업)

- multi-agent 라우팅 (legal/coding 드롭다운 + agent_id 별 base_url)
- HITL modal (다중선택 + 자유 답변)
- todo panel (옆 collapsible)
- tool output 간단 expand (chip 옆 ▶ icon, 클릭 시 펼침)
- /respond, /artifacts proxy endpoint

ax 측 SSE event spec 은 *이미 정해져 있다* (§2). UI 는 그 spec 에 맞춰
시각화. 만약 실제 ax 측 emit 형식이 spec 과 다르면 cross-check 필요 — 단
이번 commit 의 검증은 *단위 테스트 + httpx mock* 까지만 (ax 실제 띄우는 것은
별도).

---

## 2. A2A endpoint contract (ax-coding-agent 와 공유)

apt-web 이 ax 로 relay 하는 endpoint 는 모두 다음 형태:

```
GET  /healthz
GET  /.well-known/agent.json
POST /a2a/tasks/send             (sync, 짧은 작업)
POST /a2a/stream                 (SSE, 메인)
POST /a2a/respond                (HITL 답변 수신)
GET  /artifacts/{path}           (워크스페이스 산출물 다운로드)
POST /a2a, /a2a/jsonrpc, /a2a/rest  (포털 probe fallback — 필요시)
```

apt-web 측에서는 위 endpoint 를 *agent_id 별로* prefix 붙여 노출:
`/chat/a2a/{agent_id}/...`

---

## 3. SSE event 종류 (chat UI 가 시각화)

ax 측이 `/a2a/stream` 으로 emit 하는 SSE event 들. 각 frame 은 apt-legal
스타일과 같다 — 즉 한 줄 `data: {json}\n\n` 안에 `event` 필드 포함, 또는
표준 SSE `event:` 라인 + `data:` 라인 분리. 기존 chat.html 의 SSE 파서
스타일을 그대로 따르세요 (apt-legal 과 호환).

```
orchestrator.run.start          {session_id, request, started_at}
orchestrator.run.end            {session_id, success, final_response}
orchestrator.role.invoke.start  {role, description}
orchestrator.role.invoke.end    {role, success, elapsed_ms}
role.tool.call.start            {tool, brief}
role.tool.call.end              {tool, success, output_preview}
orchestrator.todo.change        {todos: [{id, content, status}]}
orchestrator.critic.verdict     {band, reason}
input_required                  {session_id, question, choices: [{id, label}], allow_free_text}
```

### HITL flow (정확히)

1. ax SubAgent 가 `ask_user_question` 호출
2. ax `/a2a/stream` 이 `input_required` SSE event emit, ax 그래프는
   `interrupt()` 로 pause
3. apt-web chat UI 가 modal 띄우고 사용자 답 받음
4. apt-web → `POST /chat/a2a/coding/respond` → ax `POST /a2a/respond`
   relay (body: `{session_id, answer}`)
5. ax 가 stored session 에서 `Command(resume=answer)` 로 그래프 재개
6. ax SSE 가 다시 흐름 (run.end 까지)

apt-web UI 는 modal 이 닫혀도 SSE 연결을 *끊지 않음* — 백엔드 ax 가 같은
스트림으로 후속 event 를 emit.

---

## 4. 작업 1 — `src/apt_web/config.py` multi-agent 확장

`AGENT_A2A_BASE_URL` (단일 string) 을 dict 로 확장. backward-compat 유지.

```python
class Settings(BaseSettings):
    # 기존 backward-compat — apt-legal 기존 배포에 영향 없음
    AGENT_A2A_BASE_URL: str = "http://localhost:8000"

    # 신규 — env override 우선
    AGENT_LEGAL_BASE_URL: str = ""    # 비어있으면 AGENT_A2A_BASE_URL 사용
    AGENT_CODING_BASE_URL: str = "http://localhost:8001"
    AGENT_DEFAULT: str = "legal"

    # 30분 (ax 장기 작업) — 기존 300 → 1800
    AGENT_A2A_TIMEOUT_SECONDS: int = 1800

    @property
    def agents(self) -> dict[str, str]:
        legal_url = self.AGENT_LEGAL_BASE_URL or self.AGENT_A2A_BASE_URL
        return {"legal": legal_url, "coding": self.AGENT_CODING_BASE_URL}
```

`agents` property 또는 동등한 helper 로 `settings.agents["legal"]` /
`settings.agents["coding"]` 접근 가능하게.

---

## 5. 작업 2 — `src/apt_web/chat/router.py` 라우팅 + 신규 endpoint

### 5.1 agent 별 라우팅으로 변환

새 패턴:
```
GET  /chat/a2a/{agent_id}/.well-known/agent.json
POST /chat/a2a/{agent_id}/tasks/send
POST /chat/a2a/{agent_id}/stream
POST /chat/a2a/{agent_id}/respond              ← 신규 (HITL)
GET  /chat/a2a/{agent_id}/artifacts/{path:path} ← 신규 (V2 산출물)
```

`agent_id` 가 `settings.agents` 에 없으면 404. 모든 핸들러는
`settings.agents[agent_id]` 로 base_url 조회.

### 5.2 Backward compatibility

기존 prefix-less endpoint (`/chat/a2a/tasks/send`, `/chat/a2a/stream`,
`/chat/.well-known/agent.json`) 도 *유지* — `agent_id="legal"` 로 라우팅
(deprecated 주석 추가). 기존 apt-legal 클라이언트 안 깨지게.

### 5.3 신규: `/respond` 핸들러

```python
@router.post("/chat/a2a/{agent_id}/respond")
async def proxy_a2a_respond(agent_id: str, request: Request) -> JSONResponse:
    base_url = settings.agents.get(agent_id)
    if not base_url:
        raise HTTPException(404, f"agent {agent_id!r} not registered")
    body = await request.body()
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=settings.AGENT_A2A_TIMEOUT_SECONDS,
    ) as client:
        upstream = await client.post(
            "/a2a/respond",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    return JSONResponse(content=upstream.json(), status_code=upstream.status_code)
```

### 5.4 신규: `/artifacts/{path}` 핸들러

`StreamingResponse` 로 ax 의 `/artifacts/{path}` 를 그대로 relay
(`media_type="application/octet-stream"`, `Content-Disposition` header
도 통과시키기).

---

## 6. 작업 3 — `src/apt_web/static/chat.html` Vue 3 UI 확장

### 6.1 Agent 선택 드롭다운

header (현재 complex_id picker 옆 또는 위) 에:
```html
<select v-model="agentId" @change="onAgentChange">
  <option value="legal">법률 상담 (apt-legal)</option>
  <option value="coding">코드 작성 (ax-coding)</option>
</select>
```

`agentId` 를 `localStorage.getItem('aptWebAgentId') || 'legal'` 로 초기화,
변경 시 `localStorage.setItem`. 모든 fetch URL 을 `/chat/a2a/${agentId}/...`
로 변경.

agent 변경 시 (onAgentChange) 메시지·todos·이벤트·hitlModal 초기화. (사용자
혼동 방지)

### 6.2 HITL 다중선택 모달

새 reactive state (Vue `ref` 또는 `reactive`):
```javascript
const hitlModal = ref({
  open: false,
  sessionId: '',
  question: '',
  choices: [],
  allowFreeText: false,
  freeText: '',
});
```

template:
```html
<div v-if="hitlModal.open" class="modal-overlay" @click.self="closeHitl">
  <div class="modal">
    <h3>{{ hitlModal.question }}</h3>
    <div class="modal-choices">
      <button v-for="c in hitlModal.choices" :key="c.id"
              class="modal-choice" @click="answerHitl(c.id)">
        {{ c.label }}
      </button>
    </div>
    <div v-if="hitlModal.allowFreeText" class="modal-free-text">
      <textarea v-model="hitlModal.freeText"
                placeholder="추가 메모 또는 자유 답변..." rows="3"></textarea>
      <button class="modal-choice modal-free-submit"
              @click="answerHitl('__free_text__')">자유 답변 보내기</button>
    </div>
  </div>
</div>
```

CSS:
```css
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.5);
  z-index: 1000; display: flex; align-items: center; justify-content: center;
}
.modal {
  background: var(--bot-bg, #fff); padding: 24px; border-radius: 8px;
  max-width: 560px; width: 90%; max-height: 80vh; overflow: auto;
}
.modal-choices {
  display: flex; flex-direction: column; gap: 8px; margin: 16px 0;
}
.modal-choice {
  padding: 10px 14px; border: 1px solid var(--border, #ddd);
  border-radius: 4px; background: #fff; cursor: pointer;
  text-align: left; font-size: 0.95rem;
}
.modal-choice:hover { background: #f5f5f5; }
.modal-free-text { margin-top: 12px; }
.modal-free-text textarea { width: 100%; padding: 8px; }
```

SSE 파서에 `input_required` 처리:
```javascript
if (evName === 'input_required') {
  const data = typeof payload.data === 'string'
    ? JSON.parse(payload.data)
    : payload.data;
  hitlModal.value = {
    open: true,
    sessionId: data.session_id,
    question: data.question,
    choices: data.choices || [],
    allowFreeText: data.allow_free_text || false,
    freeText: '',
  };
  // SSE 연결은 끊지 말 것 — 사용자 답 후 ax 가 같은 스트림에 후속 emit
}
```

`answerHitl` 함수:
```javascript
async function answerHitl(choiceId) {
  const answer = choiceId === '__free_text__'
    ? hitlModal.value.freeText
    : choiceId;
  await fetch(`/chat/a2a/${agentId.value}/respond`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      session_id: hitlModal.value.sessionId,
      answer,
    }),
  });
  hitlModal.value.open = false;
}

function closeHitl() {
  // 모달 외부 클릭 시 무시 — 사용자가 반드시 답해야 진행
  // 단, ESC 키 등으로 일부 케이스에서 닫기 허용 가능
}
```

### 6.3 Todo Panel

메시지 영역 우측에 collapsible panel (또는 좁은 화면이면 위쪽 fold-out):
```html
<aside v-if="todos.length > 0" class="todo-panel">
  <h4>작업 목록 ({{ todos.length }})</h4>
  <ul>
    <li v-for="t in todos" :key="t.id"
        :class="['todo-item', `status-${t.status}`]">
      <span class="todo-icon">{{ todoIcon(t.status) }}</span>
      <span class="todo-content">{{ t.content }}</span>
    </li>
  </ul>
</aside>
```

`todoIcon` 함수:
```javascript
function todoIcon(status) {
  return {
    pending: '○',
    in_progress: '◐',
    completed: '✓',
    failed: '✗',
  }[status] || '·';
}
```

CSS:
```css
.todo-panel {
  position: fixed; right: 16px; top: 80px; width: 280px;
  background: #fff; border: 1px solid #ddd; border-radius: 6px;
  padding: 12px; max-height: 60vh; overflow: auto; z-index: 50;
}
.todo-item { display: flex; gap: 8px; padding: 4px 0; }
.status-pending { color: #666; }
.status-in_progress { color: #0066cc; font-weight: bold; }
.status-completed { color: #16a34a; }
.status-failed { color: #dc2626; }
```

SSE 파서:
```javascript
if (evName === 'orchestrator.todo.change') {
  const data = typeof payload.data === 'string'
    ? JSON.parse(payload.data)
    : payload.data;
  todos.value = data.todos || [];
}
```

agentId 변경 또는 새 conversation 시 `todos.value = []`.

### 6.4 Tool Output 간단 expand (선택)

기존 `.event-chip` 의 `role.tool.call.end` 이벤트에 `output_preview` 가
있으면, chip 옆 `▶` icon 으로 표시하고 클릭 시 chip 아래에 `<pre>` 로 펼침.

복잡한 구현은 V2. 이번엔 다음 정도로 충분:
```javascript
// chip data structure 에 output_preview, expanded(bool) 필드 추가
// chip click 시 expanded toggle
// template 에서 chip 다음에 v-if="chip.expanded" 로 <pre>{{ chip.output_preview }}</pre>
```

CSS `pre` 영역: `max-height: 200px; overflow: auto; background: #f5f5f5; padding: 8px; font-size: 0.85em;`

### 6.5 SSE event label map 확장

기존 `eventLabel` map (대략 chat.html:712-745 근처) 에 추가:
```javascript
'orchestrator.todo.change': 'Todo 업데이트',
'input_required': '사용자 입력 필요',
```

이미 있는 항목 (`orchestrator.run.start/end`, `orchestrator.role.invoke.*`,
`role.tool.call.*`, `orchestrator.critic.verdict`) 은 *건드리지 마세요* —
동일 spec.

---

## 7. 작업 4 — 테스트

기존 `tests/test_chat.py` 가 prefix-less URL 로 테스트하면 backward-compat
확인. 안 깨지게 유지.

신규 테스트:

### `tests/test_chat_multiagent.py`

- `settings.agents` property 동작
- `/chat/a2a/legal/tasks/send` → AGENT_A2A_BASE_URL (legal) 로 relay
- `/chat/a2a/coding/tasks/send` → AGENT_CODING_BASE_URL 로 relay
- `/chat/a2a/unknown/tasks/send` → 404
- prefix-less `/chat/a2a/tasks/send` → legal 로 라우팅 (backward-compat)

httpx mock 또는 monkeypatch 로 upstream 응답 재현.

### `tests/test_chat_hitl.py`

- `/chat/a2a/coding/respond` POST → ax 의 `/a2a/respond` 로 relay
- request body 가 그대로 전달되는지
- ax 응답이 client 까지 그대로 흘러가는지
- 알 수 없는 agent 면 404

`pytest tests/` 가 모두 통과해야 함.

---

## 8. 검증

```bash
cd C:/projects/apt-web

# 1. 의존성 (이미 다 있어야 함)
python -m pytest tests/ -v

# 2. import sanity
python -c "from apt_web.main import app; print('OK')"

# 3. agents property 동작 확인
python -c "from apt_web.config import Settings; s = Settings(); print(s.agents)"
```

`pytest` 가 모두 통과해야 함. 실패 시 디버깅 후 수정.

---

## 9. 커밋 (push 는 절대 하지 말 것)

작업 단위로 commit 분리 권장:

1. `feat(config): multi-agent dict + AGENT_A2A_TIMEOUT_SECONDS 1800`
2. `feat(chat): agent_id 라우팅 + /respond + /artifacts proxy`
3. `feat(chat-ui): agent 드롭다운 + HITL modal + todo panel + tool output`
4. `test: multi-agent + HITL routing tests`

각 commit 메시지 한국어 OK. apt-web 의 `AGENTS.md` 의 commit 규칙
(Conventional Commits) 준수.

`git log --oneline -5` 로 commit 확인.

---

## 10. 산출물 보고

작업 끝나면 다음을 사용자에게 보고:

- 변경한 파일 list + 각 파일 변경 핵심 (1-2 줄)
- 새 브랜치 이름 (`feat/ax-coding-agent-integration`)
- pytest 통과 여부 (전체 출력)
- 모든 commit list (subject + sha)
- 의문/막힌 부분 — 특히 ax 측 SSE event 형식 가정한 부분 (실제 ax 구현
  align 확인 필요한 항목)

**중요 다시**: push 하지 마세요. 사용자가 검토 후 직접 push.

---

## 11. 불변 규칙

- ❌ `git push` 절대 금지 (사용자가 직접)
- ❌ 기존 `tests/test_chat.py` 등 기존 테스트 깨뜨리지 말 것
- ❌ apt-legal-agent 기존 배포 동작 깨뜨리지 말 것 (backward-compat 유지)
- ❌ `.env` 파일 git 커밋 금지
- ❌ 소스 코드 외 임의 파일 (예: README.md 대규모 재작성) 건드리지 말 것
- ✅ 한국어 소통, 영문+한글 병기 코드 주석
- ✅ 단위 테스트 추가 commit 으로 마무리
- ✅ Conventional Commits prefix
- ✅ 모든 SSE event spec 은 본 문서 §3 의 정의 그대로 따를 것 (ax 측과
  align)

전체 작업 예상 시간 — 60~90분. 너무 깊이 빠지지 말고 기능 동작에 집중,
polishing 은 V2.
