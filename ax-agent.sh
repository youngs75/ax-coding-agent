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

docker run -it --rm --network host \
  --name "$CONTAINER_NAME" \
  -e HOST_UID=$(id -u) -e HOST_GID=$(id -g) \
  -v "$WORKSPACE":/workspace \
  ax-coding-agent
