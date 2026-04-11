#!/bin/bash
set -e

# HOST_UID/HOST_GID로 사용자 생성 → 생성 파일이 호스트 권한으로 남음
if [ -n "$HOST_UID" ] && [ "$HOST_UID" != "0" ]; then
    groupadd -g "${HOST_GID:-$HOST_UID}" -o agentuser 2>/dev/null || true
    useradd -u "$HOST_UID" -g "${HOST_GID:-$HOST_UID}" -o -d /home/agentuser -m agentuser 2>/dev/null || true
    chown -R "$HOST_UID:${HOST_GID:-$HOST_UID}" /app/memory_store 2>/dev/null || true
    exec gosu agentuser python -m coding_agent.cli.app "$@"
else
    exec python -m coding_agent.cli.app "$@"
fi
