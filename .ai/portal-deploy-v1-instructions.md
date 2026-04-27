# 작업지시서 — ax-coding-agent 포털 GitLab repo 첫 부트스트랩 + 검증 + 배포 (v1)

> **대상**: 포털 GitLab `ax-coding-agent` repo 의 Web IDE Claude Code 세션
> **요청자**: VDI Claude (ax-coding-agent main 작업자)
> **전제**: VDI 측에서 GitHub `youngs75/ax-coding-agent` 의 main 에 첫 포털
>   배포 변경 4 commit 을 push 한 직후 상태. GitLab repo 는 비어있음
>   (`no commits yet`).

---

## 0. 컨텍스트 (꼭 읽고 시작)

당신은 포털 (samsungsdscoe.com / AWS EKS) 의 dev pod Web IDE 에 떠있는
Claude Code 세션입니다. 당신이 진입한 GitLab repo 는 ax-coding-agent
(LangGraph 기반 multi-agent coding harness) 의 *포털 배포 트리거 mirror*
입니다. 코드 자체는 GitHub `youngs75/ax-coding-agent` 가 단일 진실
(SSOT) 이며, 이 GitLab repo 는:

1. GitHub main 의 코드를 받아옴 (pull)
2. 포털 GitLab 에 push → 포털이 deploy 트리거 감지

**환경 정찰 결과**: dev pod 환경의 자세한 정찰은 GitHub repo 의
`.ai/portal-recon-2026-04-27.md` 에 보존돼 있음. 주요 사실:
- Debian 13 (trixie), Python 3.12+3.13, Node 22, root 권한
- LiteLLM `us.anthropic.claude-sonnet-4-6` 정상 (prefix 없음)
- bash subshell `( cmd & )` reaper 안 됨 — v22.3 처방 정당

이번 첫 부트스트랩의 목표는 **dummy daemon 이 EKS Pod 에서 떠있고
`/healthz` + `/.well-known/agent.json` 응답하는 최소 형태**. 실제 LangGraph
통합 + apt-web 통합은 다음 사이클.

한국어로 소통, 코드 주석은 영문+한글 병기 (사용자 글로벌 정책).

---

## 1. 사전 확인 (1 분)

```bash
pwd                               # /workspace/repo 또는 비슷 — 확인
git remote -v                     # origin 이 GitLab samsungsdscoe.com 인지
git log --oneline -3 2>&1         # 비어있으면 "no commits yet"
ls -la
```

위 결과가:
- cwd 가 GitLab `ax-coding-agent` repo 가 아니거나
- origin 이 `gitlab.samsungsdscoe.com/.../ax-coding-agent.git` 이 아니면

→ 즉시 멈추고 사용자에게 보고.

---

## 2. GitHub remote 추가 + main pull (3 분)

GitHub 의 ax-coding-agent main 코드를 가져와 GitLab origin 에 push 하는
첫 사이클.

### 2.1 GitHub remote 추가

```bash
git remote add github https://github.com/youngs75/ax-coding-agent.git
git remote -v
# origin → gitlab.samsungsdscoe.com/.../ax-coding-agent.git
# github → github.com/youngs75/ax-coding-agent.git
```

### 2.2 GitHub main fetch

```bash
git fetch github main
git log github/main --oneline -10
```

최근 commit 들을 보면 다음이 *최상단* 5개여야 함 (VDI 가 push 한 변경):
- `feat(deploy): Dockerfile.portal — uvicorn daemon 모드`
- `feat(web): FastAPI dummy daemon + A2A endpoints + tests`
- `feat(config): portal mode + AGENT_OBSERVABILITY 매핑 + LITELLM_MODEL_PREFIX`
- `docs(portal): 정찰 + 배포 + apt-web 통합 작업지시서`
- (이전: `docs(handoff): v22.4 ...` fb812bd)

확인 안 되면 VDI 측 push 가 아직 안 끝났을 수 있음 — 사용자에게 보고.

### 2.3 GitHub main → GitLab main 으로 첫 commit

GitLab 은 `no commits yet` 상태이므로 그대로 GitHub main 을 GitLab main 으로
복사:

```bash
git checkout -b main github/main
# 또는 이미 main 이 있으면:
# git checkout main
# git reset --hard github/main
```

이제 로컬 main 이 GitHub 와 같은 코드를 가짐.

---

## 3. 의존성 설치 + 단위 테스트 (5 분)

### 3.1 venv 생성 (정찰 §3.2 의 Python 버전 불일치 회피)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python --version          # Python 3.12.x 여야 함
pip --version             # python3.12 의 pip 여야 함
```

### 3.2 ax-coding-agent + 의존성 설치

```bash
pip install --upgrade pip
pip install -e .
```

**예상 시간**: 1-3 분. minyoung-mah 0.1.9 + langgraph + langchain + fastapi
+ uvicorn + langfuse 등 의존성 모두 PyPI 에서 설치.

설치 실패 시 (예: minyoung-mah 버전 미스매치) 즉시 보고.

### 3.3 새 web 모듈 단위 테스트

```bash
python -m pytest tests/web/ -v
```

**기대 출력** (VDI 에서 검증된 결과):
```
tests/web/test_a2a_dummy.py::test_healthz_returns_ok PASSED
tests/web/test_a2a_dummy.py::test_well_known_agent_card PASSED
tests/web/test_a2a_dummy.py::test_a2a_tasks_send_dummy PASSED
tests/web/test_a2a_dummy.py::test_a2a_probe_fallbacks[/a2a] PASSED
tests/web/test_a2a_dummy.py::test_a2a_probe_fallbacks[/a2a/jsonrpc] PASSED
tests/web/test_a2a_dummy.py::test_a2a_probe_fallbacks[/a2a/rest] PASSED
tests/web/test_a2a_dummy.py::test_a2a_respond_dummy PASSED
tests/web/test_a2a_dummy.py::test_all_responses_are_valid_json PASSED
==== 8 passed ====
```

8/8 pass 가 안 나오면 멈추고 보고.

### 3.4 import + agent card sanity

```bash
python -c "
from coding_agent.web import app, agent_card
from fastapi.testclient import TestClient
client = TestClient(app.app)
r = client.get('/healthz')
print('healthz:', r.status_code, r.json())
r = client.get('/.well-known/agent.json')
card = r.json()
print('card name:', card.get('name'))
print('card version:', card.get('version'))
print('endpoints:', card.get('endpoints'))
"
```

**기대 출력**:
- `healthz: 200 {'status': 'ok', 'version': '0.1.0'}` — version 이 `0.0.0`
  이 아닌 `0.1.0` 이어야 (`pip install -e .` 로 metadata 등록됨)
- `card name: ax-coding-agent`
- `card version: 0.1.0`
- endpoints dict 에 `tasksSend`, `tasksStream`, `respond` 3개

---

## 4. uvicorn 실제 띄워서 외부 호출 검증 (3 분)

```bash
# 백그라운드 띄움 (단순 nohup, 30초 후 죽음)
nohup python -m uvicorn coding_agent.web.app:app \
  --host 0.0.0.0 --port 8080 \
  > /tmp/ax-server.log 2>&1 &
SERVER_PID=$!
sleep 3

# healthz hit
curl -s http://localhost:8080/healthz | python -m json.tool

# agent card hit
curl -s http://localhost:8080/.well-known/agent.json | python -m json.tool

# A2A tasks/send dummy
curl -s -X POST http://localhost:8080/a2a/tasks/send \
  -H "Content-Type: application/json" -d '{"message":"hello"}' \
  | python -m json.tool

# A2A stream (첫 줄만)
curl -s -X POST http://localhost:8080/a2a/stream \
  -H "Content-Type: application/json" -d '{}' \
  --max-time 5 | head -5

# 정리
kill $SERVER_PID 2>/dev/null
wait $SERVER_PID 2>/dev/null
echo "--- server.log tail ---"
tail -20 /tmp/ax-server.log
```

기대: 5개 응답 모두 200 + 정상 JSON / SSE.

uvicorn 시작 자체가 실패하면 (예: import 에러, port 점유) 멈추고 보고.

---

## 5. GitLab origin 에 push — 포털 deploy 트리거 (2 분)

```bash
# main 이 github/main 과 동기화돼 있는지 확인
git status
git log --oneline -5

# GitLab origin 에 main push
git push -u origin main
```

push 성공 시 포털이 GitLab webhook 으로 빌드 시작 (자동) 또는 사용자가
포털 UI 에서 수동 deploy.

---

## 6. 포털 UI 작업 — 사용자 직접 (Claude 가 안 함)

push 끝나면 **사용자에게 짧게 보고**:

> "GitLab push 완료. 포털 UI 에서 다음을 진행해 주세요:
> 1. ENV 주입 (배포 화면): `LITELLM_API_KEY`, `LITELLM_BASE_URL`, `LITELLM_MODEL`,
>    `LLM_PROVIDER=litellm_portal` (4-tier 모두 sonnet-4-6 fallback). 가능하면
>    Opus 등 풍부한 KEY 주입.
> 2. Deploy 버튼 클릭
> 3. Deploy 성공 후 endpoint 등록 (외부 노출)
> 4. 외부 endpoint URL 알려주시면 외부 검증 시도하겠습니다."

---

## 7. 외부 endpoint 검증 (사용자가 endpoint 알려준 후, 5 분)

사용자가 외부 URL (예: `https://portal-serving-evangelist-1-XXX-YYY.samsungsdscoe.com`)
알려주면:

```bash
EXT_URL="https://portal-serving-evangelist-1-XXX-YYY.samsungsdscoe.com"

# 1. healthz (반드시 200)
curl -s -w "\nHTTP %{http_code} time %{time_total}s\n" "$EXT_URL/healthz"

# 2. agent card (URL 이 외부 주소로 동적 반영)
curl -s "$EXT_URL/.well-known/agent.json" | python -m json.tool

# 3. A2A tasks/send dummy
curl -s -X POST "$EXT_URL/a2a/tasks/send" \
  -H "Content-Type: application/json" -d '{}' \
  -w "\nHTTP %{http_code}\n"
```

agent card 의 `url` 필드와 endpoints URLs 가 *EXT_URL* 로 시작해야 함
(request `host`/`x-forwarded-proto` 헤더로 동적 구성된 것). `localhost` 로
나오면 reverse proxy 헤더 통과 안 되는 것 — 보고.

---

## 8. 결과 보고

각 단계마다 사용자에게 짧게 진행 상황 보고. 최종 마무리:

- 단계 1-3 (코드/테스트) 결과 — pass 또는 fail
- 단계 4 (uvicorn 띄움) 결과
- 단계 5 (GitLab push) commit hash
- 단계 7 (외부 검증) 결과 — agent card URL 동적 반영 여부 등
- 의문/막힌 부분

---

## 9. 막혔을 때 / 함정

| 증상 | 원인 가정 | 처방 |
|---|---|---|
| `pip install -e .` 가 minyoung-mah 0.1.9 못 찾음 | PyPI 캐시 또는 인덱스 | `pip install --index-url https://pypi.org/simple/ -e .` |
| pytest fail | VDI 와 환경 차이 (Python 3.13 vs 3.12 등) | venv 가 Python 3.12 인지 재확인 |
| uvicorn 시작 실패 — port in use | dev pod 에 code-server 8081 이미 떠있음 | port 8080 사용 (default). 8080 도 점유면 8082 등 |
| 외부 endpoint 503 + `awselb` | endpoint 등록 안 됨 | 포털 UI 에서 endpoint 등록 (포털 운영자 또는 사용자) |
| agent card url 이 localhost | reverse proxy 헤더 통과 안 됨 | uvicorn `--proxy-headers --forwarded-allow-ips='*'` 옵션 추가 |

---

## 10. 불변 규칙

- ❌ 소스 코드 수정 금지 (이 작업은 *검증과 배포* 만, 디버깅 발견 시 별도
  세션에서 수정)
- ❌ `git push --force` 금지
- ❌ 시크릿 (API KEY 등) 을 commit 또는 stdout 출력 금지
- ❌ GitHub remote 에 push 금지 (단일 진실은 VDI 가 GitHub 에 push)
- ✅ GitLab origin 에만 push (deploy 트리거)
- ✅ 한국어 소통
- ✅ 결과 매 단계 보고

전체 시간 — 단계 1-5 까지 약 **15-20 분**. 단계 7 은 사용자가 deploy 끝낸
후. 너무 깊이 파지 말고 *동작 확인* 이 우선.
