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

# 시스템 의존성 + gosu (사용자 전환용) + Node.js LTS + pnpm
# Node.js 는 NodeSource LTS — Debian apt 의 nodejs 가 너무 오래되어 (16~18) 최신
# create-react-app / Next.js 등이 깨짐. v20 회귀에서 Node.js 미설치로 coder 가
# create-react-app 실패 → fixer 도 sudo 없어 설치 불가 (2026-04-26).
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ripgrep tree gosu ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g pnpm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 소스 복사 + .env 복사
# NOTE: .env는 선택적 (없으면 .env.example을 fallback으로 복사).
#   - 권장: docker build 전에 `cp .env.example .env` 후 API 키 입력
#   - 대안: .env.example 기본값으로 빌드 후 runtime에 `-e KEY=value`로 주입
# COPY .env* 는 .env와 .env.example 둘 다 매칭하지만,
# .env가 없을 때도 빌드가 실패하지 않도록 .env.example을 항상 복사한 뒤
# .env가 없으면 .env.example을 .env로 복제한다.
COPY pyproject.toml ./
COPY coding_agent/ ./coding_agent/
COPY .env.example ./
COPY .env* ./
COPY entrypoint.sh ./

# minyoung-mah 라이브러리는 PyPI 미공개 — sibling checkout 을 BuildKit
# named context 로 주입한다. 빌드 명령:
#   docker buildx build --build-context minyoung_mah=../minyoung-mah \
#     -t ax-coding-agent .
COPY --from=minyoung_mah . /opt/minyoung_mah/

RUN pip install --no-cache-dir -e /opt/minyoung_mah \
    && pip install --no-cache-dir -e . \
    && mkdir -p /app/memory_store /workspace \
    && chmod +x /app/entrypoint.sh \
    && if [ ! -f /app/.env ] && [ -f /app/.env.example ]; then \
         cp /app/.env.example /app/.env; \
         echo "⚠ .env not found, copied .env.example — override API keys via docker run -e"; \
       fi

ENV PYTHONUNBUFFERED=1
ENV MEMORY_DB_PATH=/app/memory_store/memory.db

WORKDIR /workspace

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["/workspace"]
