# ═══════════════════════════════════════════════════════════════
# AX Coding Agent — Docker 이미지
#
# 사용법:
#   docker build -t ax-coding-agent .
#   docker run -it --rm --network host -v $(pwd):/workspace ax-coding-agent
# ═══════════════════════════════════════════════════════════════

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ripgrep tree \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY coding_agent/ ./coding_agent/
COPY tests/ ./tests/
COPY .env ./

RUN pip install --no-cache-dir -e .
RUN mkdir -p /app/memory_store /workspace

# 환경변수: .env에서 로드되므로 최소한만 설정
ENV PYTHONUNBUFFERED=1
ENV MEMORY_DB_PATH=/app/memory_store/memory.db

WORKDIR /workspace

ENTRYPOINT ["python", "-m", "coding_agent.cli.app"]
CMD ["/workspace"]
