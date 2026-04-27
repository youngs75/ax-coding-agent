# AGENTS.md

## Runtime
- Target runtime: PYTHON3.12
- Base language: Python 3.12
- Default package tooling: pip unless the repository already uses poetry, pip-tools, or another existing workflow
- This file is managed as the default AGENTS.md template for the PYTHON3.12 VS Code runtime workspace.

## Working Rules
- Keep changes focused on the requested feature or fix.
- Follow the existing repository structure, formatting, and dependency management instead of introducing a parallel workflow.
- Add or update a Dockerfile whenever you add or change a runnable app, MCP, agent, worker, or API service, even if Docker is not mentioned in the request.
- Add or update a runtime-appropriate .gitignore for this runtime workspace when you introduce or manage the repository.
- The .gitignore must cover generated files, virtual environments, caches, Python build artifacts, local databases, local env files, and other local-only artifacts commonly produced by the runtime.

## Dockerfile Rules
- Dockerfile and deployed container startup commands must use 0.0.0.0:8080.
- Treat port 8080 as the container-only deployment port for MCP and Agent workloads.
- For local development, use port 3000 by default unless the user explicitly requests another port or the existing project already standardizes on a different local port.
- Do not switch local development to 8080 unless there is an explicit reason to do so.
- If local and container ports differ, document the mapping in code comments, README notes, or startup scripts.

## LLM Rules
- When the implementation needs an LLM client, prefer LangChain with the LiteLLM provider.
- Read configuration from LITELLM_BASE_URL, LITELLM_API_KEY, and LITELLM_MODEL.
- Do not hardcode provider-specific URLs, API keys, or model names when those LiteLLM variables are available.
- Keep LiteLLM configuration injectable through environment variables so the same code works in local, workspace, MCP, and deployed environments.

## Observability Rules
- Support disabled mode when AGENT_OBSERVABILITY_ENABLED is not true.
- Prefer AGENT_OBSERVABILITY_* injected env vars as the canonical portal contract.
- Read observability configuration only from injected AGENT_OBSERVABILITY_* env vars:
- AGENT_OBSERVABILITY_ENABLED
- AGENT_OBSERVABILITY_PROVIDER
- AGENT_OBSERVABILITY_BASE_URL
- AGENT_OBSERVABILITY_API_KEY
- AGENT_OBSERVABILITY_SECRET_KEY
- AGENT_OBSERVABILITY_PROJECT_KEY
- AGENT_OBSERVABILITY_TRACE_NAMESPACE
- AGENT_OBSERVABILITY_TARGET_TYPE
- AGENT_OBSERVABILITY_TARGET_ID
- AGENT_OBSERVABILITY_RUNTIME_STAGE
- AGENT_OBSERVABILITY_PROJECT_ID
- AGENT_OBSERVABILITY_PROJECT_NAME
- AGENT_OBSERVABILITY_BINDING_ID
- AGENT_OBSERVABILITY_BINDING_TOKEN
- Do not hardcode Langfuse hosts, keys, project identifiers, namespaces, or tags.
- Initialize tracing from env so the same code works in workspace, MCP, and deployed environments.
- If a Langfuse SDK expects LANGFUSE_* names, map from AGENT_OBSERVABILITY_* inside that shared bootstrap module instead of reading LANGFUSE_* directly across the app.
- Build one shared observability bootstrap module for the repo instead of scattering Langfuse setup across app startup, tools, and handlers.
- Use AGENT_OBSERVABILITY_TRACE_NAMESPACE plus targetType, targetId, runtimeStage, and portal project metadata to label traces so dev and deployed activity stay separable inside the shared Langfuse project.
- Wrap request lifecycle, LLM calls, tool calls, external API calls, retries, and terminal errors in spans or generations so trace timelines stay actionable.
- Keep the tracing code non-fatal: if Langfuse init or flush fails, log safely and continue serving the request.
- Prefer masking, truncation, or structured metadata over storing raw prompts, tokens, credentials, cookies, or secrets in Langfuse payloads.
- Before finishing, verify the code behaves correctly in two modes: observability disabled and injected portal env only.
- Trace request start, LLM calls, tool calls, errors, and response completion.
- Do not store raw secrets or fully sensitive prompts/responses in traces; prefer masking or preview snippets.
- Treat namespace, tags, projectId, and runtimeStage as routing metadata, not as the security boundary.
## Delivery Checklist
- Verify the app starts in Python 3.12.
- Verify a runtime-appropriate .gitignore covers generated files and local-only config.
- Verify the Dockerfile exists and uses port 8080 for deployed execution.
- Verify local development uses port 3000 unless the repository already requires another port.
- Verify any LLM integration uses the LiteLLM environment variables.
- Verify observability code can run with AGENT_OBSERVABILITY_ENABLED=false without breaking runtime behavior.

