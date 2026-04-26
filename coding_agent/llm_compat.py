"""LLM provider compat shims — langchain-openai 가 표준 OpenAI 응답 외의
필드를 보존하지 않을 때 ax 가 보강하는 어댑터들.

현재 처리:
- **Deepseek thinking mode** (``deepseek-v4-pro`` / ``deepseek-v4-flash``):
  응답에 ``message.reasoning_content`` 가 들어가는데 langchain-openai 1.2.x
  가 이걸 ``AIMessage.additional_kwargs`` 에 보존하지 않음. tool_calls 가
  포함된 multi-turn 시나리오에서 deepseek 가 *"reasoning_content must be
  passed back"* 400 에러를 반환 (v11 회귀의 직접 원인). 다음 두 함수를
  module-level monkey-patch:

  1. ``_convert_dict_to_message`` — 응답 dict 의 ``reasoning_content`` 를
     생성된 ``AIMessage.additional_kwargs["reasoning_content"]`` 로 보존
  2. ``_convert_message_to_dict`` — 다음 호출 직렬화 시 그 값을 다시 dict
     에 포함시켜 deepseek API 가 thinking 연속성을 인지하게 함
  3. ``ChatOpenAI._create_chat_result`` — raw ``openai.ChatCompletion`` 객체
     의 ``message.reasoning_content`` (또는 ``model_extra``) 를 직접 추출해
     변환 결과에 추가 (langchain-openai 의 dict 변환에서 벗겨지는 경우 대비)

apply_compat_patches() 는 idempotent — 여러 번 호출해도 한 번만 patch.
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger("llm_compat")

_PATCH_APPLIED = False


def _extract_reasoning(raw_message: Any) -> str | None:
    """Pull ``reasoning_content`` from an ``openai.ChatCompletionMessage``-like
    object. Falls back to ``model_extra`` for forward compatibility.
    """
    if raw_message is None:
        return None
    rc = getattr(raw_message, "reasoning_content", None)
    if rc:
        return rc
    extra = getattr(raw_message, "model_extra", None)
    if isinstance(extra, dict):
        rc = extra.get("reasoning_content")
        if rc:
            return rc
    return None


def apply_compat_patches() -> None:
    """Module-level monkey-patch of langchain_openai for deepseek thinking-mode."""
    global _PATCH_APPLIED
    if _PATCH_APPLIED:
        return

    try:
        import langchain_openai.chat_models.base as _base
    except ImportError:
        log.warning("llm_compat.langchain_openai_missing")
        return

    from langchain_core.messages import AIMessage

    _orig_from_dict = _base._convert_dict_to_message
    _orig_to_dict = _base._convert_message_to_dict
    _orig_create_chat_result = _base.BaseChatOpenAI._create_chat_result

    def _patched_from_dict(_dict, *args, **kwargs):
        msg = _orig_from_dict(_dict, *args, **kwargs)
        if isinstance(msg, AIMessage):
            rc = _dict.get("reasoning_content") if isinstance(_dict, dict) else None
            if rc:
                msg.additional_kwargs["reasoning_content"] = rc
        return msg

    def _patched_to_dict(message, *args, **kwargs):
        d = _orig_to_dict(message, *args, **kwargs)
        # assistant 메시지일 때만 — additional_kwargs 의 reasoning_content 를
        # dict 로 다시 풀어 deepseek 에 송신.
        ak = getattr(message, "additional_kwargs", None) or {}
        rc = ak.get("reasoning_content")
        if rc and d.get("role") == "assistant":
            d["reasoning_content"] = rc
        return d

    def _patched_create_chat_result(self, response, generation_info=None):
        # 먼저 표준 변환 — additional_kwargs 에 reasoning_content 가 안 들어
        # 있을 수 있음 (langchain-openai 가 OpenAI 응답을 dict 로 옮길 때
        # ``model_extra`` 가 누락되는 경로 대비).
        result = _orig_create_chat_result(self, response, generation_info)
        choices = getattr(response, "choices", None) or []
        rc_count = 0
        for i, choice in enumerate(choices):
            raw_msg = getattr(choice, "message", None)
            rc = _extract_reasoning(raw_msg)
            if rc and i < len(result.generations):
                gen = result.generations[i]
                msg = getattr(gen, "message", None)
                if msg is not None and hasattr(msg, "additional_kwargs"):
                    msg.additional_kwargs.setdefault("reasoning_content", rc)
                    rc_count += 1
        log.debug(
            "llm_compat.create_chat_result_patched",
            choices=len(choices),
            reasoning_content_preserved=rc_count,
        )
        return result

    _base._convert_dict_to_message = _patched_from_dict
    _base._convert_message_to_dict = _patched_to_dict
    _base.BaseChatOpenAI._create_chat_result = _patched_create_chat_result

    _PATCH_APPLIED = True
    log.info("llm_compat.deepseek_thinking_mode_patched")


__all__ = ["apply_compat_patches"]
