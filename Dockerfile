# ═══════════════════════════════════════════════════════════════
# AX Coding Agent — Docker 이미지
#
# 사용법:
#   docker build -t ax-coding-agent .
#   docker run -it --rm \
#     -v $(pwd):/workspace \
#     -e OPENROUTER_API_KEY=sk-or-... \
#     ax-coding-agent
# ═══════════════════════════════════════════════════════════════

FROM python:3.12-slim

# 시스템 의존성 (git, ripgrep 등 도구용)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ripgrep tree \
    && rm -rf /var/lib/apt/lists/*

# 작업 디렉토리
WORKDIR /app

# 의존성 먼저 설치 (캐시 최적화)
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e . 2>/dev/null || pip install --no-cache-dir .

# 소스 복사
COPY coding_agent/ ./coding_agent/
COPY tests/ ./tests/

# 메모리 저장소 디렉토리
RUN mkdir -p /app/memory_store /workspace

# 환경변수 기본값
ENV MEMORY_DB_PATH=/app/memory_store/memory.db
ENV MAX_ITERATIONS=50
ENV LLM_TIMEOUT=60
ENV LLM_PROVIDER=openrouter
ENV PYTHONUNBUFFERED=1

# 작업 디렉토리를 /workspace로 설정 (마운트 포인트)
WORKDIR /workspace

ENTRYPOINT ["python", "-m", "coding_agent.cli.app"]
CMD ["/workspace"]
