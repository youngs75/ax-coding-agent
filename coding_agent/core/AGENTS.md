<!-- Parent: ../../AGENTS.md -->
# core/

## Purpose
에이전트 메인 루프, 상태 정의, 오케스트레이터, 오픈소스 모델 도구 호출 어댑터를 포함한다.

## Key Files
| File | Role |
|------|------|
| `loop.py` | LangGraph StateGraph 메인 루프 — 모든 노드(inject_memory, agent, tools, extract_memory, check_progress, handle_error, safe_stop) 조립 |
| `state.py` | `AgentState` TypedDict 정의 — 그래프 전체에서 공유하는 상태 |
| `orchestrator.py` | 사용자 요청 분석 → 직접 처리 vs SubAgent 위임 결정 |
| `tool_adapter.py` | 오픈소스 모델(GLM, MiniMax 등) tool calling 호환성 어댑터 — native/prompt-based 자동 전환 |
| `tool_call_utils.py` | JSON args 복구, 고아 tool_call 정리, DashScope 직렬화 보장 |

## For AI Agents
- `loop.py`의 `SYSTEM_PROMPT`가 Orchestrator 패턴을 정의한다. 메인 에이전트는 직접 코드를 작성하지 않고 `task` 도구로 SubAgent에 위임한다.
- `tool_adapter.py`의 `invoke_with_tool_fallback()`이 native tool calling 실패 시 프롬프트 기반으로 자동 폴백한다.
- `_consecutive_errors` 카운터가 3회 연속 에러 시 즉시 safe_stop한다.
