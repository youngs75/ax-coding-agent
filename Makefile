.PHONY: setup install test test-memory test-subagents test-resilience lint demo clean \
       docker-build docker-run docker-up docker-down docker-logs

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

docker-run:
	@echo "Usage: make docker-run WORKSPACE=/path/to/project"
	docker run -it --rm \
		-v $${WORKSPACE:-$$(pwd)}:/workspace \
		--env-file .env \
		ax-coding-agent

docker-up:
	docker compose up -d
	@echo ""
	@echo "=== AX Coding Agent Stack ==="
	@echo "LiteLLM Proxy: http://localhost:4000"
	@echo "Langfuse:      https://cloud.langfuse.com"
	@echo ""
	@echo "에이전트 실행:"
	@echo "  docker compose run --rm agent"
	@echo ""
	@echo "특정 프로젝트에서 작업:"
	@echo "  WORKSPACE_DIR=/path/to/project docker compose run --rm agent"

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f litellm
