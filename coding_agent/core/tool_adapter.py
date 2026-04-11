"""오픈소스 모델 Tool Calling 어댑터.

GLM, MiniMax 등 native tool calling을 지원하지 않는 모델을 위해
프롬프트 기반 도구 호출을 구현한다.

전략:
1. 모델이 native tool calling을 지원하면 → bind_tools() 사용 (기본)
2. 지원하지 않으면 → 시스템 프롬프트에 도구 스키마를 넣고,
   모델의 텍스트 응답에서 JSON tool call 블록을 파싱하여
   LangChain AIMessage.tool_calls 형식으로 변환

이를 통해 어떤 모델이든 동일한 LangGraph 파이프라인에서 동작한다.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Sequence

import structlog
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool

from coding_agent.models import supports_native_tool_calling

log = structlog.get_logger(__name__)

# ═══════════════════════════════════════════════════════════════
# 프롬프트 기반 도구 호출용 시스템 프롬프트 블록
# ═══════════════════════════════════════════════════════════════

_TOOL_PROMPT_TEMPLATE = """
## 사용 가능한 도구

아래 도구를 호출하려면 반드시 다음 JSON 형식을 사용하세요.
텍스트 응답과 도구 호출을 함께 사용할 수 있습니다.

도구 호출 형식:
```tool_call
{{"name": "도구이름", "arguments": {{"arg1": "값1", "arg2": "값2"}}}}
```

여러 도구를 호출하려면 각각 별도의 ```tool_call``` 블록을 사용하세요.

{tool_descriptions}
"""


def build_tool_prompt(tools: Sequence[BaseTool]) -> str:
    """도구 목록에서 프롬프트 기반 호출용 시스템 프롬프트 블록을 생성한다."""
    descriptions: list[str] = []
    for tool in tools:
        schema = tool.args_schema.schema() if hasattr(tool, "args_schema") and tool.args_schema else {}
        props = schema.get("properties", {})

        param_lines: list[str] = []
        for pname, pinfo in props.items():
            ptype = pinfo.get("type", "string")
            pdesc = pinfo.get("description", "")
            default = pinfo.get("default", "")
            required_marker = ""
            if pname in schema.get("required", []):
                required_marker = " (필수)"
            param_lines.append(f"    - {pname} ({ptype}){required_marker}: {pdesc}")

        desc_block = f"### {tool.name}\n{tool.description}\n"
        if param_lines:
            desc_block += "파라미터:\n" + "\n".join(param_lines)
        descriptions.append(desc_block)

    return _TOOL_PROMPT_TEMPLATE.format(tool_descriptions="\n\n".join(descriptions))


# ═══════════════════════════════════════════════════════════════
# 텍스트 응답에서 tool_call 파싱
# ═══════════════════════════════════════════════════════════════

# ```tool_call ... ``` 블록 매칭
_TOOL_CALL_PATTERN = re.compile(
    r"```tool_call\s*\n?(.*?)\n?```",
    re.DOTALL,
)

# JSON 블록 매칭 (tool_call 태그 없이도 {"name":..., "arguments":...} 감지)
_JSON_TOOL_PATTERN = re.compile(
    r'\{\s*"name"\s*:\s*"(\w+)"\s*,\s*"arguments"\s*:\s*(\{.*?\})\s*\}',
    re.DOTALL,
)


def parse_tool_calls_from_text(text: str) -> list[dict[str, Any]]:
    """LLM 텍스트 응답에서 tool_call 블록을 파싱한다.

    Returns:
        LangChain tool_calls 형식의 list:
        [{"name": str, "args": dict, "id": str, "type": "tool_call"}, ...]
    """
    tool_calls: list[dict[str, Any]] = []

    # 1차: ```tool_call``` 블록 파싱
    for match in _TOOL_CALL_PATTERN.finditer(text):
        raw = match.group(1).strip()
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "name" in parsed:
                tool_calls.append({
                    "name": parsed["name"],
                    "args": parsed.get("arguments", parsed.get("args", {})),
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "tool_call",
                })
        except json.JSONDecodeError:
            # 3단계 JSON 복구 시도
            from coding_agent.core.tool_call_utils import _try_parse_json_args
            recovered = _try_parse_json_args(raw)
            if "name" in recovered:
                tool_calls.append({
                    "name": recovered["name"],
                    "args": recovered.get("arguments", recovered.get("args", {})),
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "tool_call",
                })

    if tool_calls:
        log.info("tool_adapter.parsed_from_text", count=len(tool_calls))
        return tool_calls

    # 2차: JSON 패턴 직접 매칭 (```tool_call 없이도)
    for match in _JSON_TOOL_PATTERN.finditer(text):
        name = match.group(1)
        args_raw = match.group(2)
        try:
            args = json.loads(args_raw)
        except json.JSONDecodeError:
            from coding_agent.core.tool_call_utils import _try_parse_json_args
            args = _try_parse_json_args(args_raw)

        tool_calls.append({
            "name": name,
            "args": args if isinstance(args, dict) else {},
            "id": f"call_{uuid.uuid4().hex[:8]}",
            "type": "tool_call",
        })

    if tool_calls:
        log.info("tool_adapter.parsed_json_pattern", count=len(tool_calls))

    return tool_calls


def convert_text_response_to_tool_calls(response: AIMessage) -> AIMessage:
    """native tool calling이 없는 모델의 텍스트 응답을 tool_calls가 있는 AIMessage로 변환.

    이미 tool_calls가 있으면 그대로 반환한다.
    """
    if hasattr(response, "tool_calls") and response.tool_calls:
        return response

    content = response.content if isinstance(response.content, str) else str(response.content)
    parsed_calls = parse_tool_calls_from_text(content)

    if not parsed_calls:
        return response

    # tool_call 블록을 제거한 텍스트를 content로 유지
    clean_content = _TOOL_CALL_PATTERN.sub("", content).strip()

    return AIMessage(
        content=clean_content,
        tool_calls=parsed_calls,
    )


# ═══════════════════════════════════════════════════════════════
# 통합 어댑터: 모델에 맞는 도구 바인딩 전략 선택
# ═══════════════════════════════════════════════════════════════


def bind_tools_adaptive(model, tools: Sequence[BaseTool], model_name: str):
    """모델의 tool calling 지원 여부에 따라 적응적으로 도구를 바인딩한다.

    Returns:
        (bound_model, use_prompt_tools: bool)
        - use_prompt_tools=True면 응답 후 parse_tool_calls_from_text()를 호출해야 함
    """
    if supports_native_tool_calling(model_name):
        log.info("tool_adapter.native_binding", model=model_name)
        try:
            bound = model.bind_tools(tools)
            return bound, False
        except Exception as e:
            log.warning("tool_adapter.bind_tools_failed", model=model_name, error=str(e))
            return model, True
    else:
        log.info("tool_adapter.prompt_based", model=model_name)
        return model, True


def invoke_with_tool_fallback(
    model,
    messages: list,
    tools: Sequence[BaseTool],
    model_name: str,
    use_prompt_tools: bool,
) -> AIMessage:
    """LLM invoke를 수행하되, native tool call 실패 시 프롬프트 기반으로 재시도.

    GLM, MiniMax 등 native tool calling이 bind_tools()까지는 성공하지만
    실제 invoke 시 tool call 형식 에러가 발생하는 경우를 방어한다.

    흐름:
    1. use_prompt_tools=False → native invoke 시도
    2. invoke 성공 → 응답 반환
    3. invoke 실패 (tool 형식 에러) → 프롬프트 기반으로 재시도
    4. use_prompt_tools=True → 바로 프롬프트 기반 invoke
    """
    if use_prompt_tools:
        # 프롬프트 기반: 도구 없이 호출 후 텍스트에서 파싱
        raw_model = model  # bind_tools 안 된 모델
        response = raw_model.invoke(messages)
        return convert_text_response_to_tool_calls(response)

    try:
        response = model.invoke(messages)
        # native tool call이 있으면 그대로 반환
        if hasattr(response, "tool_calls") and response.tool_calls:
            return response
        # 텍스트만 있으면 혹시 모를 tool_call 블록 파싱 시도
        return convert_text_response_to_tool_calls(response)
    except Exception as e:
        error_str = str(e).lower()
        # tool calling 관련 에러면 프롬프트 기반으로 폴백
        tool_error_hints = (
            "tool", "function", "tool_choice", "tool_calls",
            "invalid_request", "400", "unsupported",
        )
        if any(hint in error_str for hint in tool_error_hints):
            log.warning(
                "tool_adapter.native_invoke_failed_fallback_to_prompt",
                model=model_name,
                error=str(e)[:200],
            )
            # 프롬프트 기반 재시도: 시스템 프롬프트에 도구 스키마 추가 필요
            # → 이미 agent_node에서 tool_prompt_block이 추가됨
            from langchain_core.messages import SystemMessage
            tool_prompt = build_tool_prompt(tools)

            # 시스템 메시지에 도구 프롬프트 추가
            patched = list(messages)
            if patched and isinstance(patched[0], SystemMessage):
                if tool_prompt not in patched[0].content:
                    patched[0] = SystemMessage(content=patched[0].content + "\n" + tool_prompt)

            # bind_tools 없는 원본 모델로 재시도
            from langchain_openai import ChatOpenAI
            base_model = ChatOpenAI(
                model=model.model_name if hasattr(model, "model_name") else model_name,
                api_key=model.openai_api_key if hasattr(model, "openai_api_key") else None,
                base_url=model.openai_api_base if hasattr(model, "openai_api_base") else None,
                temperature=0.0,
            )
            response = base_model.invoke(patched)
            return convert_text_response_to_tool_calls(response)
        else:
            raise
