<!-- Parent: ../../AGENTS.md -->
# resilience/

## Purpose
Agentic Loop 복원력 시스템. 7가지 장애 유형에 대한 감지/재시도/폴백/안전 중단 정책을 제공한다.

## Key Files
| File | Role |
|------|------|
| `watchdog.py` | `Watchdog` — `asyncio.wait_for` 래퍼, LLM/도구 호출 타임아웃 감시 |
| `retry_policy.py` | `FailureType` enum(7가지) + `FailurePolicy` + `ErrorClassifier` + `retry_with_backoff()` |
| `progress_guard.py` | `ProgressGuard` — 최근 N회 도구 호출 추적, 동일 액션 반복 감지 → WARN/STOP |
| `safe_stop.py` | `SafeStop` — 조건부 안전 중단 (max_iterations, 위험 경로, 커스텀 조건) |
| `error_handler.py` | `ErrorHandler` — 에러 분류 → RETRY/FALLBACK/ABORT 결정, 한국어 상태 메시지 |

## For AI Agents
- `ErrorClassifier.classify()`로 예외를 7가지 `FailureType`으로 분류한다.
- `DEFAULT_POLICIES`에 타입별 max_retries, backoff, fallback 여부가 정의되어 있다.
- `loop.py`의 `_consecutive_errors` 카운터가 3회 연속 에러 시 safe_stop을 강제한다.
- `SafeStop`의 `dangerous_path` 조건은 `.env`, `.git/`, `.ssh/` 등을 차단한다.
