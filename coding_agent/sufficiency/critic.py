"""LLM critic 호출 + JSON 응답 정규화.

apt-legal 의 ``sufficiency/critic.py`` 와 같은 자리. orchestrator 의
``invoke_role("critic", ...)`` 을 부르고, 자유 형식이 섞일 수 있는
응답 텍스트에서 첫 JSON 객체만 추출해 ``CriticVerdict`` 로 정규화한다.

JSON 파싱 / 스키마 검증이 실패하면 *escalate_hitl* 로 폴백 — 잘못된
critic 응답 때문에 무한 retry 가 도는 것을 방지.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import structlog

from coding_agent.sufficiency.schemas import CriticVerdict

if TYPE_CHECKING:
    from minyoung_mah import Orchestrator

log = structlog.get_logger("sufficiency.critic")


_VALID_VERDICTS = {"pass", "retry_lookup", "replan", "escalate_hitl"}
_VALID_TARGETS = {"coder", "fixer", "planner", "verifier", None}

# R-003 (2026-04-27) — LLM 의 자연스러운 형식 변형을 흡수해 escalate_hitl
# 폴백 비용을 줄인다. 기존 prompt 가 "JSON 한 줄, 엄격 준수" 를 강제하면서
# `"PASS"` (대문자), `"passed"`, `"OK"` 같은 사소한 변형이 무조건 escalate
# 로 빠지던 R-003 동형 사례. prompt 측 강화 대신 harness 정규화.
_VERDICT_ALIASES = {
    "pass": "pass",
    "passed": "pass",
    "ok": "pass",
    "success": "pass",
    "retry_lookup": "retry_lookup",
    "retry-lookup": "retry_lookup",
    "retry": "retry_lookup",
    "lookup": "retry_lookup",
    "replan": "replan",
    "re-plan": "replan",
    "re_plan": "replan",
    "escalate_hitl": "escalate_hitl",
    "escalate-hitl": "escalate_hitl",
    "escalate": "escalate_hitl",
    "hitl": "escalate_hitl",
    "human": "escalate_hitl",
}
_TARGET_ALIASES = {
    "coder": "coder",
    "code": "coder",
    "developer": "coder",
    "fixer": "fixer",
    "fix": "fixer",
    "planner": "planner",
    "plan": "planner",
    "verifier": "verifier",
    "verify": "verifier",
    "test": "verifier",
    "tester": "verifier",
}


def _normalize_verdict(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    return _VERDICT_ALIASES.get(raw.strip().lower())


def _normalize_target(raw: Any) -> str | None:
    """None/null 은 자체 처리, 그 외 문자열만 alias 매핑."""
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("", "null", "none"):
            return None
        return _TARGET_ALIASES.get(s)
    return None

# Match the first balanced top-level JSON object. critic prompt 는 한 줄
# JSON 만 요구하지만 LLM 이 가끔 ```json 펜스를 두르거나 짧은 머리말을
# 붙이므로 첫 ``{...}`` 블록만 잘라낸다.
_JSON_BLOCK_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)


def _extract_first_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    # ```json ... ``` 펜스 우선 시도
    fence_start = text.find("```json")
    if fence_start != -1:
        body = text[fence_start + len("```json"):]
        fence_end = body.find("```")
        if fence_end != -1:
            body = body[:fence_end]
        try:
            obj = json.loads(body.strip())
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    # 일반 패턴
    for m in _JSON_BLOCK_RE.finditer(text):
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and "verdict" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _parse_verdict(raw_text: str) -> CriticVerdict:
    """Parse the critic's free-text response into a :class:`CriticVerdict`.

    On any parse / schema failure returns an ``escalate_hitl`` verdict
    with the parse error noted in ``reason``. The harness must never
    re-loop on a malformed critic response — that's how runaway retries
    happen.
    """
    obj = _extract_first_json(raw_text or "")
    if obj is None:
        return CriticVerdict(
            verdict="escalate_hitl",
            target_role=None,
            reason=(
                "critic 응답에서 JSON 객체를 찾지 못함 — 사용자 검토 필요. "
                f"raw[:120]: {(raw_text or '')[:120]!r}"
            ),
            feedback_for_retry=None,
        )

    raw_verdict = obj.get("verdict")
    verdict = _normalize_verdict(raw_verdict)
    if verdict is None:
        return CriticVerdict(
            verdict="escalate_hitl",
            target_role=None,
            reason=f"critic 이 알 수 없는 verdict 반환: {raw_verdict!r}",
            feedback_for_retry=None,
        )

    # target alias 매핑 — 잘못된 target 은 None 으로 정규화 (verdict 자체는 살림).
    target = _normalize_target(obj.get("target_role"))

    reason = obj.get("reason") or "(critic 가 reason 을 제공하지 않음)"
    feedback = obj.get("feedback_for_retry")
    if isinstance(feedback, str) and feedback.strip().lower() == "null":
        feedback = None

    if not isinstance(reason, str):
        reason = str(reason)

    return CriticVerdict(
        verdict=verdict,  # type: ignore[arg-type]
        target_role=target,
        reason=reason,
        feedback_for_retry=feedback if isinstance(feedback, str) and feedback else None,
    )


def _build_task_summary(
    user_request: str,
    metrics: dict[str, Any],
    iteration: int,
) -> str:
    """Render the critic's task_summary — the *only* free-text input the
    role gets. Compact: it's better to keep the role focused on read_file
    spot-checks than to dump the whole conversation.
    """
    metric_lines = []
    for k in (
        "pytest_exit", "lint_errors", "todo_done", "todo_total",
        "todo_ratio",
    ):
        if k in metrics:
            metric_lines.append(f"  - {k}: {metrics[k]}")
    metrics_block = "\n".join(metric_lines) or "  (신호 없음)"
    return (
        f"## sufficiency critic — iteration {iteration}\n\n"
        f"### 사용자 원 요청\n{user_request}\n\n"
        f"### rule_gate metrics\n{metrics_block}\n\n"
        f"### 평가 지시\n"
        f"위 사용자 요청과 현재 워크스페이스 산출물을 read_file / glob_files / "
        f"grep 으로 살펴보고, 산출물이 사용자 요청을 충분히 충족하는지 "
        f"평가하라. 출력은 JSON 한 줄만. 시스템 프롬프트의 스키마를 "
        f"엄격히 준수할 것."
    )


async def invoke_critic(
    orchestrator: "Orchestrator",
    *,
    user_request: str,
    metrics: dict[str, Any],
    iteration: int,
) -> CriticVerdict:
    """Run the critic role once and return a normalised :class:`CriticVerdict`.

    Errors during invocation (timeout, missing role, etc.) are swallowed
    and converted to an ``escalate_hitl`` verdict — same fail-safe contract
    as JSON parse failures.
    """
    from minyoung_mah import InvocationContext

    invocation = InvocationContext(
        task_summary=_build_task_summary(user_request, metrics, iteration),
        user_request=user_request,
        parent_outputs={},
        shared_state={"sufficiency_iteration": iteration},
        memory_snippets=[],
        metadata={"sufficiency": True},
    )

    try:
        result = await orchestrator.invoke_role("critic", invocation)
    except Exception as exc:  # noqa: BLE001
        log.warning("sufficiency.critic.invoke_failed", error=str(exc))
        return CriticVerdict(
            verdict="escalate_hitl",
            target_role=None,
            reason=f"critic 호출 실패: {exc}",
            feedback_for_retry=None,
        )

    output = result.output
    if output is None:
        text = ""
    elif isinstance(output, str):
        text = output
    else:
        text = str(output)

    verdict = _parse_verdict(text)
    log.info(
        "sufficiency.critic.verdict",
        verdict=verdict.verdict,
        target_role=verdict.target_role,
        iteration=iteration,
    )
    return verdict


__all__ = ["invoke_critic"]
