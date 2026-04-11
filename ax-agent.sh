#!/bin/bash
# AX Coding Agent 실행 스크립트
# 사용법: ./ax-agent.sh [workspace_path]

WORKSPACE="${1:-$(pwd)}"
mkdir -p "$WORKSPACE"

docker run -it --rm --network host \
  -e HOST_UID=$(id -u) -e HOST_GID=$(id -g) \
  -v "$WORKSPACE":/workspace \
  ax-coding-agent
