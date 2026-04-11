.PHONY: setup install test test-memory test-subagents test-resilience test-performance \
       lint demo clean docker-build docker-run docker-up docker-down docker-logs

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

# ── Docker 배포 ──

docker-build:
	docker build -t ax-coding-agent .

docker-up:
	docker compose up -d litellm-db litellm
	@echo ""
	@echo "=== AX Coding Agent Stack ==="
	@echo "LiteLLM Proxy: http://localhost:4001"
	@echo "Langfuse:      https://cloud.langfuse.com"
	@echo ""
	@echo "헬스 체크 (약 30-60초 후):"
	@echo "  curl http://localhost:4001/health/liveliness"
	@echo ""
	@echo "에이전트 실행:"
	@echo "  ./ax-agent.sh [workspace_path]"

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
