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

docker run -it --rm --network host \
  --name "$CONTAINER_NAME" \
  -e HOST_UID=$(id -u) -e HOST_GID=$(id -g) \
  -e AX_PROJECT_ID="$PROJECT_ID" \
  -v "$WORKSPACE":/workspace \
  -v ax-agent-memory:/app/memory_store \
  ax-coding-agent
