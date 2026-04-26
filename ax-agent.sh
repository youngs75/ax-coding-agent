#!/bin/bash
# AX Coding Agent 실행 스크립트
# 사용법: ./ax-agent.sh [workspace_path]

WORKSPACE="${1:-$(pwd)}"
CONTAINER_NAME="ax-agent"
mkdir -p "$WORKSPACE"

# 이전에 남아있는 같은 이름의 컨테이너 정리
if docker ps -aq -f name="^${CONTAINER_NAME}$" | grep -q .; then
  echo "⚠ 기존 ax-agent 컨테이너를 정리합니다..."
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1
fi

# workspace 호스트 경로를 안정적 project_id로 해시.
# 메모리 격리(프로젝트 단위) 용도 — 같은 workspace 재방문 시에만 이전 메모리가 주입된다.
WORKSPACE_ABS="$(cd "$WORKSPACE" && pwd)"
PROJECT_ID="$(printf '%s' "$WORKSPACE_ABS" | md5sum | cut -c1-12)"

# Sufficiency loop / fixer guard / LLM provider env override — 호스트에
# 명시적으로 셋팅된 변수만 컨테이너로 forward. 미설정이면 컨테이너 내부
# default 사용. LLM provider 전환 (예: LLM_PROVIDER=deepseek + DEEPSEEK_API_KEY)
# 도 호스트에서 export 하면 자동 적용.
EXTRA_ENV=()
for v in AX_SUFFICIENCY_ENABLED AX_SUFF_MAX_ITER \
         AX_SUFF_HIGH_TODO AX_SUFF_LOW_TODO \
         AX_SUFF_HIGH_PRD AX_SUFF_LOW_PRD \
         AX_FIXER_RETRY_WARN \
         LLM_PROVIDER \
         DEEPSEEK_API_KEY DEEPSEEK_BASE_URL \
         DASHSCOPE_API_KEY DASHSCOPE_BASE_URL \
         OPENROUTER_API_KEY \
         LITELLM_PROXY_URL LITELLM_MASTER_KEY \
         REASONING_MODEL STRONG_MODEL DEFAULT_MODEL FAST_MODEL \
         ORCHESTRATOR_TIER \
         LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY; do
  if [ -n "${!v}" ]; then
    EXTRA_ENV+=(-e "$v=${!v}")
  fi
done

docker run -it --rm --network host \
  --name "$CONTAINER_NAME" \
  -e HOST_UID=$(id -u) -e HOST_GID=$(id -g) \
  -e AX_PROJECT_ID="$PROJECT_ID" \
  "${EXTRA_ENV[@]}" \
  -v "$WORKSPACE":/workspace \
  -v ax-agent-memory:/app/memory_store \
  ax-coding-agent
