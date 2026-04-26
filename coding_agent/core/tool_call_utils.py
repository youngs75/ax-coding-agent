"""오픈소스 모델 호환 도구 호출 유틸리티.

Qwen, GLM 등 오픈소스 모델에서 발생하는 tool calling 문제를 방어한다:

1. JSON 이중 닫힘 괄호 (}}) — Qwen3 계열
2. DashScope의 JSON 문자열 args 요구
3. flash/turbo 모델의 tool_choice 미지원
4. 고아 tool_call ↔ ToolMessage 정합성
5. 빈 이름/잘못된 형식의 tool call 필터링

참고: youngs75_coding_ai_agent/coding_agent/core/tool_call_utils.py 패턴 차용
"""

from __future__ import annotations

import json
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════
# 1. 도구 호출 필드 안전 추출
# ═══════════════════════════════════════════════════════════════


def tc_name(tool_call: Any) -> str | None:
    """도구 호출 객체에서 이름을 안전하게 추출한다.

    dict, OpenAI function format, LangChain 객체 등 다양한 형태를 처리.
    """
    if tool_call is None:
        return None
    if isinstance(tool_call, dict):
        name = tool_call.get("name")
        if name:
            return str(name)
        fn = tool_call.get("function")
        if isinstance(fn, dict):
            return fn.get("name")
        return tool_call.get("type")
    for attr in ("name", "tool_name"):
        val = getattr(tool_call, attr, None)
        if val:
            return str(val)
    fn = getattr(tool_call, "function", None)
    if fn:
        return getattr(fn, "name", None)
    return None


def tc_id(tool_call: Any) -> str | None:
    """도구 호출에서 ID를 추출한다."""
    if tool_call is None:
        return None
    if isinstance(tool_call, dict):
        return tool_call.get("id") or tool_call.get("tool_call_id")
    return getattr(tool_call, "id", None) or getattr(tool_call, "tool_call_id", None)


# ═══════════════════════════════════════════════════════════════
# 2. JSON args 안전 파싱 (Qwen3 이중 괄호 방어)
# ═══════════════════════════════════════════════════════════════


def _try_parse_json_args(raw: str) -> dict[str, Any]:
    """JSON 문자열을 파싱하되, 실패 시 3단계 복구를 시도한다.

    Qwen3 등 일부 모델이 이중 닫기 중괄호(}})나
    불필요한 후행 문자를 생성하는 문제를 방어한다.
    """
    # 1차: 원본 파싱
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        pass

    # 2차: 후행 중괄호/공백 제거 후 재시도
    stripped = raw.rstrip()
    while stripped.endswith("}}"):
        stripped = stripped[:-1]
        try:
            parsed = json.loads(stripped)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            continue

    # 3차: 첫 번째 '{' ~ 마지막 '}'만 추출하여 재시도
    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = raw[first_brace : last_brace + 1]
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            pass

    log.warning("tool_call_utils.json_parse_failed", raw=raw[:200])
    return {}


def tc_args(tool_call: Any) -> dict[str, Any]:
    """도구 호출에서 인자를 추출한다. JSON 문자열도 안전 파싱."""
    if tool_call is None:
        return {}
    raw: Any = None
    if isinstance(tool_call, dict):
        raw = tool_call.get("args") or tool_call.get("arguments")
        if raw is None:
            fn = tool_call.get("function")
            if isinstance(fn, dict):
                raw = fn.get("arguments")
    else:
        raw = getattr(tool_call, "args", None) or getattr(tool_call, "arguments", None)
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return _try_parse_json_args(raw)
    return {}


# ═══════════════════════════════════════════════════════════════
# 3. 고아 tool_call ↔ ToolMessage 정합성 정리
# ═══════════════════════════════════════════════════════════════


def sanitize_messages_for_llm(messages: list[Any]) -> list[Any]:
    """양방향 고아 tool_call/ToolMessage를 정리한다.

    DashScope/OpenAI API는 AIMessage.tool_calls와 ToolMessage가 반드시
    1:1 대응해야 한다. 이 함수는 양방향으로 정리한다:

    1. 대응 ToolMessage가 없는 AIMessage의 tool_calls → 해당 call만 제거
    2. 대응 AIMessage가 없는 ToolMessage → 제거
    """
    from langchain_core.messages import AIMessage, ToolMessage

    # 1단계: 양방향 ID 수집
    ai_call_ids: set[str] = set()
    tool_result_ids: set[str] = set()

    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                cid = tc_id(tc)
                if cid:
                    ai_call_ids.add(cid)
        elif isinstance(msg, ToolMessage):
            tcid = getattr(msg, "tool_call_id", None)
            if tcid:
                tool_result_ids.add(tcid)

    # 2단계: 정리
    cleaned: list[Any] = []
    removed_count = 0

    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            valid_calls = [
                tc for tc in msg.tool_calls if tc_id(tc) in tool_result_ids
            ]
            if len(valid_calls) == len(msg.tool_calls):
                cleaned.append(msg)
            elif valid_calls:
                valid_ids = {tc_id(tc) for tc in valid_calls}
                ak = dict(getattr(msg, "additional_kwargs", {}) or {})
                ak_tcs = ak.get("tool_calls", [])
                if ak_tcs:
                    ak["tool_calls"] = [
                        tc for tc in ak_tcs if tc.get("id", "") in valid_ids
                    ]
                cleaned.append(
                    AIMessage(
                        content=msg.content or "",
                        tool_calls=valid_calls,
                        additional_kwargs=ak,
                        id=getattr(msg, "id", None),
                    )
                )
                removed_count += len(msg.tool_calls) - len(valid_calls)
            else:
                cleaned.append(AIMessage(content=msg.content or "[도구 호출 생략됨]"))
                removed_count += len(msg.tool_calls)
            continue

        if isinstance(msg, ToolMessage):
            tcid = getattr(msg, "tool_call_id", None)
            if tcid and tcid not in ai_call_ids:
                removed_count += 1
                continue

        cleaned.append(msg)

    if removed_count:
        log.info("tool_call_utils.sanitized_orphans", removed=removed_count)

    # Anthropic strict ordering: tool_use AIMessage 직후 *immediately*
    # ToolMessage 가 와야 함. 다른 메시지(SystemMessage / HumanMessage /
    # 또 다른 tool_call AIMessage 등) 가 끼면 400 거부 (v15 회귀).
    # 직후 ToolMessage 가 없는 AIMessage 의 tool_calls 는 텍스트로 강등.
    final: list[Any] = []
    i = 0
    while i < len(cleaned):
        msg = cleaned[i]
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            expected_ids = {tc_id(tc) for tc in msg.tool_calls if tc_id(tc)}
            # 다음 메시지(들) 이 ToolMessage 이고 모든 expected_ids 가 채워지는지
            seen_ids: set[str] = set()
            j = i + 1
            while j < len(cleaned) and isinstance(cleaned[j], ToolMessage):
                tcid = getattr(cleaned[j], "tool_call_id", None)
                if tcid:
                    seen_ids.add(tcid)
                j += 1
            if seen_ids >= expected_ids:
                final.append(msg)
            else:
                # 짝 부족 — tool_calls 강등 (anthropic 거부 방지)
                final.append(AIMessage(content=msg.content or "[도구 호출 짝 부족 — 강등됨]"))
                log.info(
                    "tool_call_utils.tool_use_pair_demoted",
                    expected=len(expected_ids),
                    seen=len(seen_ids),
                )
        else:
            final.append(msg)
        i += 1

    return final


# ═══════════════════════════════════════════════════════════════
# 4. DashScope 호환 직렬화 보장
# ═══════════════════════════════════════════════════════════════


def ensure_tool_calls_serializable(messages: list[Any]) -> list[Any]:
    """AIMessage의 tool_calls가 DashScope/OpenAI 호환 형식인지 보장한다.

    LangChain 내부 형식(tool_calls[].args: dict)은 DashScope가 직렬화하지 못한다.
    additional_kwargs.tool_calls가 없으면 생성하여 arguments를 JSON 문자열로 보장한다.
    """
    from langchain_core.messages import AIMessage

    result: list[Any] = []
    for msg in messages:
        if not isinstance(msg, AIMessage) or not getattr(msg, "tool_calls", None):
            result.append(msg)
            continue

        ak = getattr(msg, "additional_kwargs", {}) or {}
        ak_tcs = ak.get("tool_calls", [])

        if ak_tcs:
            fixed = False
            for tc in ak_tcs:
                fn = tc.get("function", {})
                args = fn.get("arguments")
                if isinstance(args, dict):
                    fn["arguments"] = json.dumps(args, ensure_ascii=False)
                    fixed = True
            if fixed:
                new_ak = dict(ak)
                new_ak["tool_calls"] = ak_tcs
                result.append(
                    AIMessage(
                        content=msg.content or "",
                        tool_calls=msg.tool_calls,
                        additional_kwargs=new_ak,
                        id=getattr(msg, "id", None),
                    )
                )
            else:
                result.append(msg)
        else:
            new_ak_tcs = []
            for tc in msg.tool_calls:
                tc_id_val = tc.get("id", "")
                tc_name_val = tc.get("name", "")
                tc_args_val = tc.get("args", {})
                args_str = (
                    json.dumps(tc_args_val, ensure_ascii=False)
                    if isinstance(tc_args_val, dict)
                    else str(tc_args_val)
                )
                new_ak_tcs.append(
                    {
                        "id": tc_id_val,
                        "type": "function",
                        "function": {"name": tc_name_val, "arguments": args_str},
                    }
                )
            new_ak = dict(ak)
            new_ak["tool_calls"] = new_ak_tcs
            result.append(
                AIMessage(
                    content=msg.content or "",
                    tool_calls=msg.tool_calls,
                    additional_kwargs=new_ak,
                    id=getattr(msg, "id", None),
                )
            )

    return result


# ═══════════════════════════════════════════════════════════════
# 5. 잘못된 tool call 필터링
# ═══════════════════════════════════════════════════════════════


def filter_invalid_tool_calls(messages: list[Any]) -> list[Any]:
    """빈 이름이나 잘못된 형식의 tool_calls를 제거한다."""
    from langchain_core.messages import AIMessage

    result: list[Any] = []
    for msg in messages:
        if not isinstance(msg, AIMessage) or not getattr(msg, "tool_calls", None):
            result.append(msg)
            continue

        valid = [tc for tc in msg.tool_calls if tc_name(tc)]
        if len(valid) == len(msg.tool_calls):
            result.append(msg)
        elif valid:
            ak = dict(getattr(msg, "additional_kwargs", {}) or {})
            valid_ids = {tc_id(tc) for tc in valid}
            ak_tcs = ak.get("tool_calls", [])
            if ak_tcs:
                ak["tool_calls"] = [
                    tc for tc in ak_tcs if tc.get("id", "") in valid_ids
                ]
            result.append(
                AIMessage(
                    content=msg.content or "",
                    tool_calls=valid,
                    additional_kwargs=ak,
                    id=getattr(msg, "id", None),
                )
            )
            log.info("tool_call_utils.filtered_invalid", removed=len(msg.tool_calls) - len(valid))
        else:
            result.append(AIMessage(content=msg.content or "[잘못된 도구 호출 제거됨]"))
            log.warning("tool_call_utils.all_calls_invalid")

    return result


# ═══════════════════════════════════════════════════════════════
# 6. 메시지 전처리 파이프라인 (모든 정리를 순서대로 적용)
# ═══════════════════════════════════════════════════════════════


def prepare_messages_for_llm(messages: list[Any]) -> list[Any]:
    """LLM 호출 전 메시지 전처리 파이프라인.

    순서:
    1. 잘못된 tool call 필터링
    2. 고아 tool_call/ToolMessage 정리
    3. DashScope 호환 직렬화 보장
    """
    messages = filter_invalid_tool_calls(messages)
    messages = sanitize_messages_for_llm(messages)
    messages = ensure_tool_calls_serializable(messages)
    return messages
