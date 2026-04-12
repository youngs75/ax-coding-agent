.PHONY: setup install test test-memory test-subagents test-resilience test-performance \
       lint demo clean up down logs \
       docker-build docker-run docker-up docker-down docker-logs traces trace

# ── 로컬 개발 ──

setup: install
	@echo "Setup complete."

install:
	pip install -e ".[dev]" 2>/dev/null || pip install -e .

test:
	python -m pytest tests/ -v

test-memory:
	python -m pytest tests/test_memory.py -v

test-subagents:
	python -m pytest tests/test_subagents.py -v

test-resilience:
	python -m pytest tests/test_resilience.py -v

test-performance:
	python -m pytest tests/test_performance.py -v

lint:
	ruff check coding_agent/ tests/
	ruff format --check coding_agent/ tests/

demo:
	python -m coding_agent.cli.app

clean:
	rm -rf memory_store/*.db
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# ══════════════════════════════════════════════════════════════
# ── 원클릭 실행 (권장 경로) ──
#
# 평가자/신규 사용자는 아래 두 단계로 테스트 가능:
#   1. cp .env.example .env   (편집해서 DASHSCOPE_API_KEY 입력)
#   2. make up                (이미지 빌드 + Gateway 기동 + healthy 대기)
#   3. ./ax-agent.sh          (대화형 CLI)
# ══════════════════════════════════════════════════════════════

up:
	@echo "◆ AX Coding Agent — 빌드 및 기동 시작"
	@echo ""
	@if [ ! -f .env ]; then \
	  echo "⚠  .env 파일이 없습니다."; \
	  echo "   다음 명령을 먼저 실행하세요:"; \
	  echo "     cp .env.example .env"; \
	  echo "     (편집기로 .env 열어서 DASHSCOPE_API_KEY 입력)"; \
	  exit 1; \
	fi
	docker compose up -d --build litellm-db litellm agent
	@echo ""
	@echo "⏳ LiteLLM Gateway healthy 체크 중 (최대 120초)..."
	@timeout 120 bash -c 'until [ "$$(docker inspect --format={{.State.Health.Status}} ax-litellm-proxy 2>/dev/null)" = "healthy" ]; do sleep 2; done' || (echo "⚠  Gateway 기동 실패 — docker compose logs litellm 확인"; exit 1)
	@echo "✓ Gateway ready"
	@echo ""
	@echo "════════════════════════════════════════"
	@echo "  ◆ AX Coding Agent — READY"
	@echo "════════════════════════════════════════"
	@echo ""
	@echo "  대화형 실행:"
	@echo "    ./ax-agent.sh [workspace_path]"
	@echo ""
	@echo "  상태 확인:"
	@echo "    curl http://localhost:4001/health/liveliness"
	@echo "    make logs"
	@echo ""
	@echo "  종료:"
	@echo "    make down"
	@echo ""

down:
	docker compose down

logs:
	docker compose logs -f litellm

# ── Docker 개별 제어 (레거시) ──

docker-build:
	docker compose build agent

docker-up:
	docker compose up -d litellm-db litellm

docker-run:
	@echo "Usage: ./ax-agent.sh [workspace_path]"
	./ax-agent.sh

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f litellm

# ── Langfuse 트레이스 ──

traces:
	python -m coding_agent.utils.langfuse_trace_exporter --list-traces 10

trace:
	@echo "Usage: make trace ID=<trace-id>"
	python -m coding_agent.utils.langfuse_trace_exporter --trace $(ID) -v
