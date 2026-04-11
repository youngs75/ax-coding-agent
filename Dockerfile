# ═══════════════════════════════════════════════════════════════
# AX Coding Agent — Docker 이미지
#
# 사용법:
#   docker build -t ax-coding-agent .
#   docker run -it --rm --network host \
#     -e HOST_UID=$(id -u) -e HOST_GID=$(id -g) \
#     -v $(pwd):/workspace ax-coding-agent
# ═══════════════════════════════════════════════════════════════

FROM python:3.12-slim

# 시스템 의존성 + gosu (사용자 전환용)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ripgrep tree gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 소스 + .env 복사
COPY pyproject.toml ./
COPY coding_agent/ ./coding_agent/
COPY .env ./
COPY entrypoint.sh ./

RUN pip install --no-cache-dir -e . \
    && mkdir -p /app/memory_store /workspace \
    && chmod +x /app/entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV MEMORY_DB_PATH=/app/memory_store/memory.db

WORKDIR /workspace

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["/workspace"]
