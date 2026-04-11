"""LiteLLM 기반 4-Tier 모델 팩토리.

모든 LLM 호출은 이 모듈을 통해 수행한다.
LiteLLM이 OpenRouter/DashScope/OpenAI 등 다양한 프로바이더를 통합 지원한다.

오픈소스 모델 호환성:
    - GLM, MiniMax 등 native tool calling 미지원 모델은 prompt-based 폴백
    - flash/turbo 모델의 tool_choice 미지원 대응
    - 모델별 특성 자동 감지
"""

from __future__ import annotations

import os
from typing import Literal

import structlog
import litellm
from langchain_openai import ChatOpenAI

from coding_agent.config import get_config

log = structlog.get_logger(__name__)

# LiteLLM 로깅 최소화
litellm.suppress_debug_info = True

TierName = Literal["reasoning", "strong", "default", "fast"]


# ═══════════════════════════════════════════════════════════════
# 모델별 tool calling 호환성 프로필
# ═══════════════════════════════════════════════════════════════

# ── 모델별 tool calling 호환성 프로필 ──
#
# OpenRouter를 통한 오픈소스 모델은 크게 3가지로 분류:
#
# A. Native tool calling 완전 지원 (Qwen-coder, Llama-3 등)
#    → bind_tools() 사용, 추가 처리 불필요
#
# B. Native tool calling 지원하지만 quirks 있음 (GLM-5.1, Nemotron 등)
#    → bind_tools() 시도 → 실패 시 프롬프트 기반 폴백
#    → JSON args 파싱 복구 (tool_call_utils.py)
#    → tool_choice 미사용
#
# C. Native tool calling 미지원 (일부 MiniMax, DeepSeek-R1 등)
#    → 프롬프트 기반 도구 호출만 사용

# 그룹 C: native tool calling 아예 미지원 → 프롬프트 기반만 사용
_NO_NATIVE_TOOL_CALLING: tuple[str, ...] = (
    "deepseek-r1",      # DeepSeek R1 (reasoning only, no tool use)
)

# tool_choice 파라미터를 지원하지 않는 모델 패턴
# → bind_tools()는 가능하지만 tool_choice="required" 등은 불가
_NO_TOOL_CHOICE: tuple[str, ...] = (
    "flash",
    "turbo",
    "lite",
    "mini",
    "glm",
    "minimax",
    "nemotron",
)

# 그룹 B: native tool calling은 되지만 args JSON 형식이 불안정한 모델
# → tool_call_utils._try_parse_json_args()로 3단계 복구 적용
_QUIRKY_TOOL_CALLING: tuple[str, ...] = (
    "glm",
    "minimax",
    "nemotron",
    "qwen",  # Qwen도 간혹 이중 괄호 발생
)


def supports_native_tool_calling(model_name: str) -> bool:
    """해당 모델이 native tool calling (function_calling)을 지원하는지 판단."""
    model_lower = model_name.lower()
    return not any(p in model_lower for p in _NO_NATIVE_TOOL_CALLING)


def supports_tool_choice(model_name: str) -> bool:
    """해당 모델이 tool_choice 파라미터를 지원하는지 판단."""
    model_lower = model_name.lower()
    return not any(p in model_lower for p in _NO_TOOL_CHOICE)


def _strip_provider_prefix(model_name: str) -> str:
    """LiteLLM 라우팅 접두사를 제거한다.

    예: 'openrouter/z-ai/glm-5.1' → 'z-ai/glm-5.1'
        'dashscope/qwen3-max' → 'qwen3-max'
    """
    prefixes = ("openrouter/", "dashscope/")
    for prefix in prefixes:
        if model_name.startswith(prefix):
            return model_name[len(prefix):]
    return model_name


# Cache model instances by (tier, temperature) to avoid recreating
# HTTP connections for every SubAgent call.
_model_instance_cache: dict[tuple[str, float], ChatOpenAI] = {}


def get_model(tier: TierName = "default", temperature: float = 0.0) -> ChatOpenAI:
    """지정된 티어의 LLM 인스턴스를 반환한다.

    동일한 (tier, temperature) 조합은 캐시된 인스턴스를 재사용하여
    HTTP 커넥션 재생성 오버헤드를 제거한다.
    """
    cache_key = (tier, temperature)
    if cache_key in _model_instance_cache:
        return _model_instance_cache[cache_key]

    cfg = get_config()
    model_tier = cfg.model_tier
    raw_model_name = getattr(model_tier, tier)

    # LiteLLM Proxy 모드: Docker 하니스로 LLM Gateway 경유
    # → 모든 호출이 LiteLLM을 거치며 Langfuse로 자동 트레이싱됨
    if cfg.litellm_proxy_url:
        model_name = raw_model_name
        api_key = cfg.litellm_master_key or "sk-harness-local-dev"
        base_url = cfg.litellm_proxy_url

        log.debug("models.get_model.litellm_proxy", tier=tier, model=model_name, proxy=base_url)

        instance = ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            timeout=cfg.llm_timeout,
        )
        _model_instance_cache[cache_key] = instance
        return instance

    # 직접 프로바이더 모드 (기본)
    model_name = _strip_provider_prefix(raw_model_name)

    if cfg.provider == "dashscope":
        os.environ.setdefault("DASHSCOPE_API_KEY", cfg.dashscope_api_key)
        api_key = cfg.dashscope_api_key
        base_url = cfg.dashscope_base_url
    else:
        os.environ.setdefault("OPENROUTER_API_KEY", cfg.openrouter_api_key)
        api_key = cfg.openrouter_api_key
        base_url = "https://openrouter.ai/api/v1"

    log.debug("models.get_model", tier=tier, model=model_name, provider=cfg.provider)

    instance = ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        timeout=cfg.llm_timeout,
    )
    _model_instance_cache[cache_key] = instance
    return instance


def get_model_name(tier: TierName = "default") -> str:
    """티어에 해당하는 모델 이름 반환."""
    cfg = get_config()
    return getattr(cfg.model_tier, tier)


# 폴백 체인: reasoning → strong → default → fast
FALLBACK_ORDER: list[TierName] = ["reasoning", "strong", "default", "fast"]


def get_fallback_model(current_tier: TierName, temperature: float = 0.0) -> ChatOpenAI | None:
    """현재 티어보다 한 단계 낮은 폴백 모델 반환. 더 이상 없으면 None."""
    try:
        idx = FALLBACK_ORDER.index(current_tier)
    except ValueError:
        return None
    if idx + 1 >= len(FALLBACK_ORDER):
        return None
    return get_model(FALLBACK_ORDER[idx + 1], temperature)
