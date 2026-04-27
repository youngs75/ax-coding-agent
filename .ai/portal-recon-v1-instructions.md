# 작업지시서 — 포털 EKS 환경 정찰 (v1)

> **대상**: 포털 Web IDE 의 Claude Code 세션
> **요청자**: VDI Claude (ax-coding-agent main 작업자)
> **전제**: 사용자가 사내 포털(samsungsdscoe.com) 에 새 Repo 를 생성하고
>   해당 Repo 의 Web IDE(devcontainer 기반 VS Code) 를 띄운 직후 상태.
> **목적**: ax-coding-agent 를 이 환경에 배포하기 전에, **실제 컨테이너가
>   어떤 OS/도구/네트워크/디스크/권한을 가지는지** 정확히 파악.

---

## 0. 컨텍스트 (꼭 읽고 시작)

당신(Web IDE Claude)은 사내 교육 포털 환경 안의 devcontainer 에서 돌고
있습니다. 이 컨테이너가 곧 ax-coding-agent 가 배포될 EKS Pod 의 *기준*
이 됩니다 (정확히 같은 Pod 는 아니지만 같은 base 이미지·같은 네트워크
정책·같은 stroage class 를 사용한다고 가정).

ax-coding-agent 는 LangGraph 기반 multi-agent harness 로, 컨테이너 안에서
*자식 프로세스* (`npm install`, `pytest`, `node server.js`, `git`, `ripgrep`
등) 를 직접 실행해서 코드를 작성·검증합니다. 따라서 단순 LLM 호출 에이전트
와 달리 **자식 프로세스 실행 권한, 워크스페이스 디스크, 네트워크 egress**
가 모두 중요합니다.

VDI 측에서는 raw TCP egress 가 차단돼 있어서 컨테이너를 직접 띄울 수
없으므로, 이 정찰은 *사용자가 복사해서 당신에게 전달* 하는 형태로
이뤄집니다. 결과 파일을 GitHub 에 push 하면 VDI Claude 가 받아서 Phase 1
배포 작업을 설계합니다.

## 1. 사전 확인 (1-2 분)

```bash
# 1.1 현재 위치
pwd
ls -la

# 1.2 git remote
git remote -v
git log --oneline -3 2>/dev/null || echo "no commits yet"

# 1.3 사용자 / hostname
whoami
id
hostname
```

기록할 것: cwd 경로, git remote URL, 사용자 이름, hostname.

## 2. 환경 정찰 — 10 개 섹션

각 섹션마다 명령어를 실행하고, **출력 그대로** 결과 파일에 붙여 넣으세요.
명령어가 실패하면 에러 메시지도 그대로 기록 (그 자체가 정보).

### 2.1 OS / 커널 / 컨테이너 정체

```bash
uname -a
cat /etc/os-release
cat /proc/1/cgroup | head -5
cat /proc/self/status | grep -E '^(Name|Pid|Uid|Gid|CapEff|Seccomp)'
ls -la /.dockerenv 2>/dev/null || echo "no .dockerenv"
```

### 2.2 사용 가능한 런타임·도구 버전

```bash
for tool in python python3 pip uv pipx node npm pnpm yarn deno bun \
            git gh ripgrep rg jq curl wget docker podman \
            make gcc g++ go rustc java psql redis-cli; do
  if command -v "$tool" >/dev/null 2>&1; then
    version=$("$tool" --version 2>&1 | head -1)
    echo "✓ $tool: $version"
  else
    echo "✗ $tool: not installed"
  fi
done
```

### 2.3 Python 환경 상세

```bash
python3 -c "import sys; print(sys.version); print(sys.prefix); print(sys.executable)"
python3 -m pip list 2>/dev/null | head -50
which uv && uv --version
ls /usr/local/lib/python3*/site-packages 2>/dev/null | head -20
```

### 2.4 Node.js 환경 상세

```bash
node -v
npm -v
npm config get prefix
npm config get registry
npm list -g --depth=0 2>/dev/null | head -20
```

### 2.5 환경 변수 (포털이 미리 주입한 것)

```bash
# 전체 env 중에서 LLM/observability/포털 관련만 추리기 (시크릿 본문은 마스킹)
env | grep -iE '^(LITELLM|LANGFUSE|OPENAI|ANTHROPIC|AWS|HTTP|HTTPS|NO_PROXY|PORT|HOME|USER|PATH|PYTHON|UV|NODE|MCP|APT|AGENT|PORTAL|LANG|TZ)' \
  | sed -E 's/(KEY|TOKEN|SECRET|PASSWORD)=.{8,}/\1=***MASKED***/' \
  | sort
```

**중요**: API KEY/TOKEN 본문은 위 sed 가 마스킹합니다. 만약 마스킹이 안 된
변수가 보이면 직접 `***MASKED***` 로 바꿔서 기록하세요. 시크릿 본문 절대
GitHub push 금지.

### 2.6 네트워크 — egress 가능 endpoint 확인

```bash
# 포털 내부 서비스
for url in https://litellm.samsungsdscoe.com/health \
           https://langfuse.samsungsdscoe.com \
           https://portal-serving-evangelist-1-mcp-c94cc9c5.samsungsdscoe.com/healthz \
           https://portal-serving-evangelist-1-mcp2-c01386b6.samsungsdscoe.com/healthz; do
  echo "=== $url ==="
  curl -sS -o /dev/null -w "HTTP %{http_code}, time %{time_total}s\n" \
    --max-time 10 "$url" 2>&1
done

# 외부 — 코드 작성 시 npm install / pip install 가능한지
for url in https://registry.npmjs.org/ \
           https://pypi.org/simple/ \
           https://github.com \
           https://api.openai.com \
           https://api.anthropic.com; do
  echo "=== $url ==="
  curl -sS -o /dev/null -w "HTTP %{http_code}, time %{time_total}s\n" \
    --max-time 10 "$url" 2>&1
done

# raw TCP (참고용 — VDI 가 막혀있는 거랑 다른지 확인)
python3 -c "
import socket
for host, port in [('www.google.com', 443), ('github.com', 22), ('pypi.org', 443)]:
    try:
        s = socket.create_connection((host, port), timeout=5)
        print(f'✓ {host}:{port} TCP OK')
        s.close()
    except Exception as e:
        print(f'✗ {host}:{port} {type(e).__name__}: {e}')
"
```

### 2.7 디스크 — 쓰기 가능 경로와 용량

```bash
df -h
mount | grep -E '(rw|ro)' | head -20

# 쓰기 가능 여부 테스트
for dir in / /tmp /home /workspace /data /var/tmp /opt /app; do
  if [ -d "$dir" ]; then
    test_file="$dir/.write_test_$$"
    if touch "$test_file" 2>/dev/null; then
      rm -f "$test_file"
      size=$(df -h "$dir" 2>/dev/null | tail -1 | awk '{print $4}')
      echo "✓ $dir writable, free: $size"
    else
      echo "✗ $dir NOT writable"
    fi
  else
    echo "- $dir does not exist"
  fi
done
```

### 2.8 권한 — sub-process spawn / 시그널 / cgroup

```bash
# subprocess 가능?
python3 -c "
import subprocess, os
r = subprocess.run(['ls', '/'], capture_output=True, text=True)
print('subprocess.run rc=', r.returncode, 'stdout lines=', len(r.stdout.splitlines()))
"

# fork bomb 방지 한도 (process limit)
ulimit -a

# sudo 가능?
sudo -n true 2>&1 || echo "no sudo"

# cgroup 상의 메모리/CPU 한도
cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null || echo "no memory cgroup"
cat /sys/fs/cgroup/cpu.max 2>/dev/null || echo "no cpu cgroup v2"
nproc
```

### 2.9 포트 / 네트워크 listen

```bash
# 어떤 포트가 이미 listen 중?
ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null || echo "no ss/netstat"

# 환경변수에 PORT 가 정해져 있나?
echo "PORT=$PORT"

# 외부에 노출되는 포트가 ENV 어딘가에 적혀 있나?
env | grep -iE 'PORT|LISTEN|BIND|HOST' | head -10
```

### 2.10 포털 특이 파일 / agent 정의

```bash
# agent.yaml 또는 agent card 흔적
find / -maxdepth 5 -name "agent.yaml" -o -name "agent.yml" -o -name "agent.json" 2>/dev/null | head -10
find / -maxdepth 5 -path "*/.well-known/agent.json" 2>/dev/null | head -5

# devcontainer 정의
cat .devcontainer/devcontainer.json 2>/dev/null || echo "no devcontainer.json in cwd"

# 포털이 미리 깔아둔 readme/스크립트
ls -la /workspace 2>/dev/null
ls -la /opt 2>/dev/null
ls -la /etc/portal 2>/dev/null

# 이미 떠있는 프로세스 (포털 sidecar 확인)
ps -ef | head -20
```

## 3. 추가 확인 — ax-coding-agent 시뮬레이션 가능성 (5 분)

ax-coding-agent 가 실제로 이 환경에서 *동작할 수 있는지* 빠른 sanity:

```bash
# 작업용 임시 디렉토리
WORK=$(mktemp -d)
cd "$WORK"

# 3.1 npm install + build 시뮬 (ax 의 coder 가 실제로 함)
mkdir test-react && cd test-react
cat > package.json <<'EOF'
{"name":"test","version":"0.1.0","scripts":{"build":"echo built"}}
EOF
time npm install --no-audit --no-fund 2>&1 | tail -5
echo "---"
cd ..

# 3.2 pip install 시뮬
mkdir test-py && cd test-py
time pip install --quiet requests 2>&1 | tail -5
python3 -c "import requests; print('requests', requests.__version__)"
cd ..

# 3.3 Long-running child + reaper 동작 (ax 의 v22.3 처방 검증)
# 30 초 sleep 백그라운드 spawn 후 30 초 뒤 잘 죽는지
( sleep 30 & echo "spawned PID=$!" )
sleep 35
ps aux | grep "[s]leep 30" || echo "✓ background sleep already reaped"

# 3.4 git clone 가능?
time git clone --depth 1 https://github.com/octocat/Hello-World.git /tmp/hello-test 2>&1 | tail -3
rm -rf /tmp/hello-test

# 3.5 LiteLLM proxy 직접 호출 (env 에 KEY 가 있다는 가정)
# 만약 LITELLM_API_KEY 가 보이면 단발 호출 시도. 없으면 skip 하고 보고만.
if [ -n "$LITELLM_API_KEY" ] || [ -n "$LITELLM_MASTER_KEY" ]; then
  KEY="${LITELLM_API_KEY:-$LITELLM_MASTER_KEY}"
  curl -sS -X POST https://litellm.samsungsdscoe.com/v1/chat/completions \
    -H "Authorization: Bearer $KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"openai/us.anthropic.claude-sonnet-4-6","messages":[{"role":"user","content":"reply with the single word OK"}],"max_tokens":10}' \
    --max-time 30 2>&1 | head -20
else
  echo "no LITELLM key in env — skipped"
fi

cd / && rm -rf "$WORK"
```

## 4. 결과 기록 형식

위 모든 명령의 출력을 **하나의 마크다운 파일** `RECON_REPORT.md` 에 다음
구조로 기록하세요. 출력은 가급적 그대로 (잘라내지 말고). 길어도 OK —
정찰의 핵심은 *생 데이터*.

```markdown
# 포털 EKS 환경 정찰 보고서 — <YYYY-MM-DD>

## 0. 컨텍스트
- Repo: <name>
- 포털 endpoint (만약 노출됐다면): <url>
- 보고 시각: <UTC timestamp>

## 1. 사전 확인
[1.1, 1.2, 1.3 출력]

## 2. 환경 정찰
### 2.1 OS / 커널
[출력]
### 2.2 도구 버전
[출력]
### 2.3 Python
[출력]
### 2.4 Node.js
[출력]
### 2.5 환경 변수 (시크릿 마스킹됨)
[출력]
### 2.6 네트워크
[출력]
### 2.7 디스크
[출력]
### 2.8 권한
[출력]
### 2.9 포트
[출력]
### 2.10 포털 특이 파일
[출력]

## 3. ax-coding-agent 시뮬레이션
[3.1 ~ 3.5 출력]

## 4. 종합 진단

(여기는 *당신* 이 위 데이터를 보고 1-2 문단 자유 요약)

- ax-coding-agent 가 이 환경에서 돌 수 있을 것으로 보이는가?
- 가장 큰 리스크 / 미해결 의문은 무엇인가?
- VDI Claude 에게 추가로 확인을 부탁하면 좋은 항목 (있다면)
```

## 5. 완료 후

1. `RECON_REPORT.md` 를 git 에 add 후 commit (subject: `docs(recon): portal EKS environment v1`)
2. `git push origin main`
3. 사용자에게 짧게 보고: "정찰 완료, RECON_REPORT.md 푸시했습니다. VDI 측에서 받아갈 수 있습니다."

## 6. 불변 규칙 (지켜주세요)

- ❌ **시크릿 본문 (KEY/TOKEN/SECRET/PASSWORD) 을 파일에 기록하거나 commit
  하지 마세요**. 마스킹 sed 가 빠뜨린 게 보이면 수동 마스킹.
- ❌ 소스 코드 임의 수정 금지 (devcontainer 설정 변경 금지). 정찰만.
- ❌ 포털 시스템 디렉토리 (`/etc`, `/usr`, `/opt/portal`) 에 파일 쓰기 금지.
- ❌ 정찰 중 발견한 long-running 프로세스 죽이지 마세요 (포털이 띄운 것
  일 수 있음).
- ✅ 결과 파일은 `RECON_REPORT.md` 단 하나로.
- ✅ 모든 소통은 한국어.
- ✅ 명령 실행 중 에러가 나도 *기록하고 계속* 진행 (에러 자체가 정보).

## 7. 막혔을 때

위 명령 중 권한·네트워크 문제로 *전체 섹션* 이 막히면:
- 그 섹션은 "FAILED — <이유>" 만 기록하고 다음 섹션으로 진행.
- 마지막 § 4 종합 진단에 명시.
- 사용자에게 "X 섹션 막힘, VDI Claude 에게 우회 방법 문의 필요" 라고 요청.

전체 30 분 안에 끝내는 게 목표. 너무 깊이 파지 말고 *생 데이터 회수* 가
우선.
