# 포털 EKS 환경 정찰 보고서 — 2026-04-27

> 사용자가 사내 포털(samsungsdscoe.com / AWS EKS) 에 ax-coding-agent 를
> 배포하기 전, dev pod 의 Web IDE Claude Code 가 수행한 환경 정찰 결과.
> 작업지시서: `.ai/portal-recon-v1-instructions.md`.

## 0. 컨텍스트
- Repo: ax-coding-agent
- Git remote: `https://gitlab.samsungsdscoe.com/74435f2f-2053-4d88-b00e-55e2b2d92bf0/ax-coding-agent.git`
- 포털 endpoint: samsungsdscoe.com (GitLab + LiteLLM + Langfuse + MCP)
- 보고 시각: 2026-04-27T01:09Z
- devcontainer 이미지: `755035179509.dkr.ecr.us-east-1.amazonaws.com/mspsa/agent-portal/agent-portal-vscode-python312:latest`

## 1. 사전 확인

### 1.1 현재 위치
```
/workspace/repo
total 24
drwxr-xr-x. 4 50259 50259 6144 Apr 27 00:51 .
drwx------. 9 50259 50259 6144 Apr 27 00:59 ..
drwxr-xr-x. 2 50259 50259 6144 Apr 27 00:51 .devcontainer
drwxr-xr-x. 7 50259 50259 6144 Apr 27 00:51 .git
-rw-r--r--. 1 50259 50259 4721 Apr 27 00:51 AGENTS.md
```

### 1.2 git remote
```
origin	https://gitlab.samsungsdscoe.com/74435f2f-2053-4d88-b00e-55e2b2d92bf0/ax-coding-agent.git (fetch)
origin	https://gitlab.samsungsdscoe.com/74435f2f-2053-4d88-b00e-55e2b2d92bf0/ax-coding-agent.git (push)
no commits yet
```

### 1.3 사용자 / hostname
```
whoami: root
uid=0(root) gid=0(root) groups=0(root)
hostname: ws-74435f-c4e543-7a3e9c-57b7fc4897-7cc2h
```

## 2. 환경 정찰

### 2.1 OS / 커널
```
Linux ws-74435f-c4e543-7a3e9c-57b7fc4897-7cc2h 6.12.68-92.122.amzn2023.x86_64 #1 SMP PREEMPT_DYNAMIC Mon Feb  9 10:43:10 UTC 2026 x86_64 GNU/Linux

PRETTY_NAME="Debian GNU/Linux 13 (trixie)"
NAME="Debian GNU/Linux"
VERSION_ID="13"
VERSION="13 (trixie)"
VERSION_CODENAME=trixie
DEBIAN_VERSION_FULL=13.4
ID=debian

/proc/1/cgroup:
0::/

/proc/self/status (발췌):
Name:	cat
Pid:	3160
Uid:	0	0	0	0
Gid:	0	0	0	0
CapEff:	00000000a80425fb
Seccomp:	0
Seccomp_filters:	0

no .dockerenv
```

참고: `.dockerenv` 없음 — containerd 기반 EKS Pod. Seccomp 비활성. cgroup v2.

### 2.2 도구 버전
```
✓ python: Python 3.12.13
✓ python3: Python 3.13.5
✓ pip: pip 25.0.1 from /usr/local/lib/python3.12/site-packages/pip (python 3.12)
✗ uv: not installed
✗ pipx: not installed
✓ node: v22.22.2
✓ npm: 10.9.7
✗ pnpm: not installed
✗ yarn: not installed
✗ deno: not installed
✗ bun: not installed
✓ git: git version 2.47.3
✓ gh: gh version 2.46.0 (2025-01-13 Debian 2.46.0-3)
✗ ripgrep: not installed (binary name)
✓ rg: ripgrep 14.1.1 (rev 0a466a11ee)
✗ jq: not installed
✓ curl: curl 8.14.1 (x86_64-pc-linux-gnu) libcurl/8.14.1 OpenSSL/3.5.5
✓ wget: GNU Wget 1.25.0
✗ docker: not installed
✗ podman: not installed
✓ make: GNU Make 4.4.1
✓ gcc: gcc (Debian 14.2.0-19) 14.2.0
✓ g++: g++ (Debian 14.2.0-19) 14.2.0
✗ go: not installed
✗ rustc: not installed
✗ java: not installed
✗ psql: not installed
✗ redis-cli: not installed
```

### 2.3 Python
```
python3 (기본): 3.13.5 (prefix=/usr, exec=/usr/bin/python3)
python (python3.12): 3.12.13

pip 은 python3.12 용:
  pip 25.0.1 from /usr/local/lib/python3.12/site-packages/pip (python 3.12)

/usr/local/lib/python3.12/site-packages:
  README.txt, pip, pip-25.0.1.dist-info

uv: not installed
```

**주의**: `python3` → 3.13, `pip` → 3.12 로 불일치. pip install 한 패키지는 python3.12 에서만 import 가능. python3 (3.13) 에서는 별도 pip 필요하거나 venv 사용해야 함.

### 2.4 Node.js
```
node: v22.22.2
npm: 10.9.7
prefix: /usr
registry: https://registry.npmjs.org/

글로벌 패키지:
├── @anthropic-ai/claude-code@2.1.112
├── corepack@0.34.6
├── npm@10.9.7
└── opencode-ai@1.4.7
```

### 2.5 환경 변수 (시크릿 마스킹됨)
```
AGENT_OBSERVABILITY_API_KEY=***MASKED***
AGENT_OBSERVABILITY_BASE_URL=https://langfuse.samsungsdscoe.com
AGENT_OBSERVABILITY_BINDING_ID=b91cea86-43d2-4d35-8054-ed17f7ec9930
AGENT_OBSERVABILITY_BINDING_TOKEN=***MASKED***
AGENT_OBSERVABILITY_ENABLED=true
AGENT_OBSERVABILITY_PROJECT_ID=74435f2f-2053-4d88-b00e-55e2b2d92bf0
AGENT_OBSERVABILITY_PROJECT_KEY=***MASKED***
AGENT_OBSERVABILITY_PROJECT_NAME=[Evangelist-1] 김영석 / AGENT2
AGENT_OBSERVABILITY_PROVIDER=langfuse
AGENT_OBSERVABILITY_RUNTIME_STAGE=dev
AGENT_OBSERVABILITY_SECRET_KEY=***MASKED***
AGENT_OBSERVABILITY_TARGET_ID=f5922ce1-bc65-46e3-9bb8-13ea020415df
AGENT_OBSERVABILITY_TARGET_TYPE=workspace_dev_agent
AGENT_OBSERVABILITY_TRACE_NAMESPACE=project:74435f2f-2053-4d88-b00e-55e2b2d92bf0:workspace_dev_agent:f5922ce1-bc65-46e3-9bb8-13ea020415df:dev
ANTHROPIC_AUTH_TOKEN=***MASKED***
ANTHROPIC_BEDROCK_BASE_URL=https://litellm.samsungsdscoe.com/bedrock
ANTHROPIC_MODEL=us.anthropic.claude-sonnet-4-6
HOME=/workspace
LANG=C.UTF-8
LITELLM_API_KEY=***MASKED***
LITELLM_BASE_URL=https://litellm.samsungsdscoe.com
LITELLM_MODEL=
MCP_CONNECTION_NONBLOCKING=true
NODE_EXEC_PATH=/usr/lib/code-server/lib/node
NoDefaultCurrentDirectoryInExePath=1
PATH=/usr/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
PYTHON_SHA256=c08bc65a81971c1dd5783182826503369466c7e67374d1646519adf05207b684
PYTHON_VERSION=3.12.13
```

### 2.6 네트워크
#### 포털 내부 서비스
```
=== https://litellm.samsungsdscoe.com/health ===
HTTP 401, time 0.033s
=== https://langfuse.samsungsdscoe.com ===
HTTP 200, time 0.029s
=== https://portal-serving-evangelist-1-mcp-c94cc9c5.samsungsdscoe.com/healthz ===
HTTP 200, time 0.027s
=== https://portal-serving-evangelist-1-mcp2-c01386b6.samsungsdscoe.com/healthz ===
HTTP 200, time 0.091s
```

#### 외부 서비스
```
=== https://registry.npmjs.org/ ===
HTTP 200, time 0.039s
=== https://pypi.org/simple/ ===
HTTP 200, time 0.142s
=== https://github.com ===
HTTP 200, time 0.038s
=== https://api.openai.com ===
HTTP 421, time 0.033s
=== https://api.anthropic.com ===
HTTP 404, time 0.037s
```

#### Raw TCP
```
✓ www.google.com:443 TCP OK
✓ github.com:22 TCP OK
✓ pypi.org:443 TCP OK
```

결론: 내부·외부 모두 egress 열려있음. npm, pip, git clone 모두 가능.

### 2.7 디스크
```
Filesystem      Size  Used Avail Use% Mounted on
overlay         100G   11G   89G  11% /
tmpfs            64M     0   64M   0% /dev
127.0.0.1:/     8.0E  282G  8.0E   1% /workspace
/dev/nvme0n1p1  100G   11G   89G  11% /etc/hosts
shm              64M     0   64M   0% /dev/shm
tmpfs            31G   12K   31G   1% /run/secrets/kubernetes.io/serviceaccount
tmpfs            16G     0   16G   0% /proc/acpi
tmpfs            16G     0   16G   0% /sys/firmware
```

마운트 요약:
- `/` : overlay, rw, 89G 여유
- `/workspace` : NFS4 (127.0.0.1 via nfs4, port 20906), rw, 8EB (사실상 무제한)
- `/dev/shm` : 64M

쓰기 테스트:
```
✓ / writable, free: 89G
✓ /tmp writable, free: 89G
✓ /home writable, free: 89G
✓ /workspace writable, free: 8.0E
- /data does not exist
✓ /var/tmp writable, free: 89G
✓ /opt writable, free: 89G
- /app does not exist
```

### 2.8 권한
```
subprocess.run rc= 0 stdout lines= 20   (✓ 자식 프로세스 정상 실행)

ulimit -a:
real-time non-blocking time  (microseconds, -R) unlimited
core file size              (blocks, -c) unlimited
data seg size               (kbytes, -d) unlimited
file size                   (blocks, -f) unlimited
pending signals                     (-i) 30446
max locked memory           (kbytes, -l) unlimited
open files                          (-n) 1048576
pipe size                (512 bytes, -p) 8
POSIX message queues         (bytes, -q) 819200
stack size                  (kbytes, -s) 10240
cpu time                   (seconds, -t) unlimited
max user processes                  (-u) unlimited
virtual memory              (kbytes, -v) unlimited
file locks                          (-x) unlimited

sudo: command not found (no sudo binary)

cgroup:
memory.max: max (무제한)
cpu.max: max 100000 (무제한 — 100000µs period 기본)
nproc: 8
```

### 2.9 포트
```
ss/netstat: not installed

PORT= (비어있음)

관련 ENV:
KUBERNETES_SERVICE_PORT_HTTPS=443
KUBERNETES_SERVICE_PORT=443
WS_646D8D_73392F_2A3142_PORT_8080_TCP_PORT=8080
WS_8C57B9_E5B095_EC4068_PORT_8080_TCP_PORT=8080
HOSTNAME=ws-74435f-c4e543-7a3e9c-57b7fc4897-7cc2h
WS_8C57B9_E5B095_EC4068_SERVICE_PORT=8080
WS_564C94_613B0F_3B2707_PORT_8080_TCP=tcp://172.20.223.169:8080
WS_74435F_C4E543_7A3E9C_PORT_8080_TCP=tcp://172.20.135.253:8080
WS_E0AAE9_8C2CC0_8AC857_PORT_8080_TCP_PROTO=tcp
```

참고: code-server 가 0.0.0.0:8081 에 listen 중 (PID 1 entrypoint 에서 확인).

### 2.10 포털 특이 파일
```
agent.yaml / agent.yml / agent.json: 없음
.well-known/agent.json: 없음
```

devcontainer.json:
```json
{
  "name": "python3.12 workspace",
  "image": "755035179509.dkr.ecr.us-east-1.amazonaws.com/mspsa/agent-portal/agent-portal-vscode-python312:latest",
  "customizations": {
    "vscode": {
      "settings": {
        "files.autoSave": "afterDelay",
        "terminal.integrated.defaultProfile.linux": "bash"
      },
      "extensions": [
        "ms-python.python",
        "ms-python.vscode-pylance",
        "ms-toolsai.jupyter"
      ]
    }
  },
  "remoteUser": "root"
}
```

/workspace 내용:
```
drwxr-xr-x. 2 50259 50259 6144 .agent-portal
drwxr-xr-x. 4 50259 50259 6144 .cache
drwx------. 8 50259 50259 6144 .claude
-rw-------. 1 50259 50259  231 .claude.json
drwxr-xr-x. 5 50259 50259 6144 .config
-rw-r--r--. 1 50259 50259  134 .gitconfig
drwxr-xr-x. 3 50259 50259 6144 .local
drwxr-xr-x. 3 50259 50259 6144 .npm
drwxr-xr-x. 5 50259 50259 6144 repo
```

/opt: 빈 디렉토리
/etc/portal: 존재하지 않음

실행 중인 프로세스:
```
PID 1   : sh -c ... (entrypoint — git config + code-server 실행)
PID 31  : code-server --auth none --bind-addr 0.0.0.0:8081
PID 50  : code-server (node worker)
PID 62  : fileWatcher
PID 84  : extensionHost
PID 461 : markdown-language-features
PID 475 : ptyHost
PID 492 : bash (terminal 1)
PID 1748: run-jedi-language-server.py
PID 1749: bash (terminal 2)
PID 2093: claude (Claude Code extension — v2.1.120)
PID 2105: jsonServerMain
```

## 3. ax-coding-agent 시뮬레이션

### 3.1 npm install + build
```
up to date in 228ms

real	0m0.299s
user	0m0.307s
sys	0m0.066s
```
✓ npm install 정상 동작

### 3.2 pip install
```
pip install → python3.12 site-packages 에 설치됨 (requests 2.33.1)
python3 (3.13) 에서는 import 실패 — ModuleNotFoundError
python3.12 에서는 import 성공: requests 2.33.1

real	0m1.621s
```
⚠ pip 은 3.12 용. python3 기본이 3.13 이므로 ax-coding-agent 에서는 명시적으로 python3.12 사용하거나, venv 생성 필요.

### 3.3 Long-running child + reaper
```
spawned PID=4216
(sleep 5 뒤 ps 에 sleep 프로세스 잔존 — bash subshell 내 백그라운드 프로세스가 정리 안 됨)
```
⚠ bash subshell `( sleep 5 & )` 의 자식이 완전히 정리되지 않음. ax-coding-agent v22.3 처방 (shell.py guard) 의 EKS 환경 재현 사례.

### 3.4 git clone
```
Cloning into '/tmp/hello-test'...

real	0m0.189s
```
✓ git clone 정상, 매우 빠름 (< 200ms)

### 3.5 LiteLLM proxy 호출 — **결정적 발견**
첫 시도 (모델명 `openai/us.anthropic.claude-sonnet-4-6`):
```json
{"error":{"message":"key not allowed to access model. This key can only access models=['cohere-embed-4', 'cohere-rerank-3-5', 'us.anthropic.claude-sonnet-4-6']. Tried to access openai/us.anthropic.claude-sonnet-4-6","type":"key_model_access_denied","param":"model","code":"401"}}
```

재시도 (모델명 `us.anthropic.claude-sonnet-4-6`):
```json
{"id":"chatcmpl-74f9efd8-def7-464f-85d5-ef7910cfacde","created":1777252136,"model":"us.anthropic.claude-sonnet-4-6","object":"chat.completion","choices":[{"finish_reason":"stop","index":0,"message":{"content":"OK","role":"assistant"}}],"usage":{"completion_tokens":4,"prompt_tokens":13,"total_tokens":17}}
```
✓ LiteLLM 정상 동작.

**dev pod 의 KEY 허용 모델 목록**: `cohere-embed-4`, `cohere-rerank-3-5`, `us.anthropic.claude-sonnet-4-6`. **serving pod 는 별도 KEY 주입 가정** (Opus 등 풍부한 모델 가능, 사용자 확인).

**모델 ID prefix 정책 — apt-legal 과 반대**: apt-legal 의 KEY 는 `openai/` prefix 등록, ax dev pod KEY 는 prefix 없이 등록. KEY 별로 다름.

## 4. 종합 진단

### ax-coding-agent 이 환경에서 돌 수 있는가?

**기본적으로 가능하다.** 이 컨테이너는 Debian 13 (trixie) 기반, root 권한, cgroup 제한 없음(메모리·CPU 무제한), 오픈 파일 100만 개, 프로세스 수 무제한, 디스크 충분(overlay 89G + NFS 사실상 무제한). 네트워크 egress 가 내부·외부 모두 열려있어서 npm registry, PyPI, GitHub 클론 모두 잘 된다. LiteLLM 프록시도 올바른 모델명(`us.anthropic.claude-sonnet-4-6`)으로 정상 호출된다. subprocess spawn 도 문제없다.

### 가장 큰 리스크 / 미해결 의문

1. **Python 버전 불일치**: `python3` 가 3.13 인데 `pip` 은 3.12 용. ax-coding-agent 가 `python3` 로 실행하면 pip 으로 설치한 패키지를 못 찾음. **해결**: venv 기반으로 통일하거나, `python3.12` 를 명시적으로 사용.
2. **dev pod KEY 모델 제한**: dev pod 의 LITELLM_API_KEY 로는 sonnet-4-6 하나만 호출 가능. **serving pod 는 별도 KEY 주입** (포털 UI ENV 기능) — Opus 등 풍부한 모델 가능.
3. **jq 미설치**: JSON 파싱용 `jq` 가 없음. `apt install jq` 로 설치 가능 (root 권한 있고 apt repo 접근 가능).
4. **pnpm/yarn 미설치**: npm 만 있음. ax-coding-agent 가 pnpm/yarn 프로젝트를 다룬다면 설치 필요.
5. **NFS 기반 /workspace**: NFS4 마운트라 대량 I/O 시 latency 이슈 가능. node_modules 같은 대량 파일 I/O 는 overlay `/tmp` 에서 하는 것이 나을 수 있음.
6. **ss/netstat 미설치**: 포트 listen 확인이 불가. 디버깅 시 불편.
7. **Langfuse Observability 환경변수 사전 주입됨**: `AGENT_OBSERVABILITY_*` 변수들이 이미 세팅돼 있어서 ax-coding-agent 의 tracing 설정과 충돌 또는 활용 가능.
8. **Web IDE 와 serving 분리**: dev pod 는 Web IDE (code-server :8081) 가 떠있는 환경. serving pod 는 별도 — code-server 없는 minimal daemon 이미지 가능.

### serving pod 에 대한 가정 (사용자 확인)

- **별도 EKS Pod** 로 떠짐 — dev pod 와 분리
- **포털 UI 의 ENV 기능** 으로 KEY/모델/기타 변수 주입 가능 — Opus 등 풍부한 KEY 가능
- 환경 자체는 dev pod 와 거의 같음 (Debian 13 + Python 3.12/3.13 + Node 22 + 같은 base image 가정)
- Web IDE 는 외부 접근 불가 — GitHub (개발) ↔ GitLab (포털 배포 트리거) dual remote 패턴 사용
