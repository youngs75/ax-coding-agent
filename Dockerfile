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

# 시스템 의존성
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ripgrep tree \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 소스 전체 복사 후 설치
COPY pyproject.toml ./
COPY coding_agent/ ./coding_agent/
COPY tests/ ./tests/

# 패키지 설치 (editable 모드로 — /app에서 import 가능)
RUN pip install --no-cache-dir -e .

# 메모리 저장소
RUN mkdir -p /app/memory_store /workspace

# 환경변수 기본값
ENV MEMORY_DB_PATH=/app/memory_store/memory.db
ENV MAX_ITERATIONS=50
ENV LLM_TIMEOUT=60
ENV LLM_PROVIDER=openrouter
ENV PYTHONUNBUFFERED=1

WORKDIR /workspace

ENTRYPOINT ["python", "-m", "coding_agent.cli.app"]
CMD ["/workspace"]
