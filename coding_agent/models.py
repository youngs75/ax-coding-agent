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
from typing import Any, Literal

import structlog
import litellm
from langchain_openai import ChatOpenAI

# LLM provider compat shims — deepseek thinking-mode 의 reasoning_content 가
# multi-turn tool-calling 시나리오에서 보존되도록 langchain-openai 를
# 모듈 로드 시 한 번 patch. import 순서 보존을 위해 ChatOpenAI import 직후.
from coding_agent.llm_compat import apply_compat_patches as _apply_llm_compat
_apply_llm_compat()

from coding_agent.config import get_config

log = structlog.get_logger(__name__)

# LiteLLM 로깅 최소화
litellm.suppress_debug_info = True

TierName = Literal["reasoning", "strong", "default", "fast"]


# ═══════════════════════════════════════════════════════════════
# 티어별 max_tokens (출력 상한)
# ═══════════════════════════════════════════════════════════════
#
# Anthropic Messages API는 max_tokens가 필수 파라미터. LiteLLM 프록시가
# default를 넣어주지만 값이 작으면 tool_use 블록(특히 write_todos 같이
# args가 큰 도구)이 중간에 잘려 tool_calls=None으로 루프가 종료되는
# 회귀가 v10 Claude E2E에서 관찰됨.
#
# Claude 4.6 세대 표준 상한 (beta 헤더 없음):
#   claude-opus-4-6   : 128K output
#   claude-sonnet-4-6 :  64K output
#   claude-haiku-4-5  :  64K output
#
# qwen3 계열은 DashScope default가 충분하지만, 일관성을 위해 tier별로
# 동일한 상한을 전 provider에 적용. 상한은 실제 응답 길이를 강제하지
# 않고(모델이 알아서 조절) 잘림 방지용 천장만 제공.
_TIER_MAX_TOKENS: dict[str, int] = {
    "reasoning": 32_768,  # planner — opus 급 설계 작업
    "strong":    32_768,  # coder/orchestrator — 큰 tool_use args 여유
    "default":   16_384,
    "fast":       8_192,  # verifier/extractor — 짧은 구조화 응답
}


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


# ── Langfuse LLM-level tracing ────────────────────────────────────────────
# litellm proxy 모드는 proxy 의 ``success_callback=["langfuse"]`` 가 LLM
# generation span 을 자동 발화 → 이 콜백은 *직접 provider* 모드에서만 부착.
# 이중 발화 방지를 위해 cfg.litellm_proxy_url 이 설정된 경로에서는 호출 안 함.
_langfuse_handler_singleton: Any | None = None
_langfuse_init_attempted: bool = False


def _get_langfuse_callbacks() -> list[Any]:
    """Langfuse LangChain CallbackHandler singleton (lazy).

    LANGFUSE_PUBLIC_KEY/SECRET_KEY 미설정이거나 SDK 초기화 실패 시 빈 list.
    한 번 시도해서 실패하면 재시도 안 함.
    """
    global _langfuse_handler_singleton, _langfuse_init_attempted
    if _langfuse_init_attempted:
        return [_langfuse_handler_singleton] if _langfuse_handler_singleton else []
    _langfuse_init_attempted = True
    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        return []
    try:
        from langfuse.langchain import CallbackHandler

        _langfuse_handler_singleton = CallbackHandler()
        log.info("models.langfuse_callback.enabled")
    except Exception as exc:  # noqa: BLE001
        log.warning("models.langfuse_callback.init_failed", error=str(exc))
    return [_langfuse_handler_singleton] if _langfuse_handler_singleton else []


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
    max_tokens = _TIER_MAX_TOKENS.get(tier)

    # LiteLLM Proxy 모드: Docker 하니스로 LLM Gateway 경유
    # → 모든 호출이 LiteLLM을 거치며 Langfuse로 자동 트레이싱됨
    if cfg.litellm_proxy_url:
        model_name = raw_model_name
        api_key = cfg.litellm_master_key or "sk-harness-local-dev"
        base_url = cfg.litellm_proxy_url

        litellm_kwargs: dict[str, Any] = {}
        # GLM / deepseek 계열은 reasoning_content 를 반환하는 thinking 모델 →
        # langchain-openai 1.2.x 의 ``_convert_chunk_to_generation_chunk`` 가
        # reasoning_content 를 무시 + chunk timeout 유발. 비-streaming 으로 강제.
        # (deepseek 직접 호출 분기와 동일한 워크어라운드. v16 1차 시도에서
        # ``langchain_openai.stream_chunk_timeout`` 회귀로 발견 — 2026-04-26)
        _lower = model_name.lower()
        if any(k in _lower for k in ("glm", "deepseek", "qwen3-max", "reasoner")):
            litellm_kwargs["disable_streaming"] = True

        log.debug(
            "models.get_model.litellm_proxy",
            tier=tier,
            model=model_name,
            proxy=base_url,
            max_tokens=max_tokens,
            disable_streaming=litellm_kwargs.get("disable_streaming", False),
        )

        instance = ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            timeout=cfg.llm_timeout,
            max_tokens=max_tokens,
            **litellm_kwargs,
        )
        _model_instance_cache[cache_key] = instance
        return instance

    # 직접 프로바이더 모드 (기본)
    model_name = _strip_provider_prefix(raw_model_name)
    extra_kwargs: dict[str, Any] = {}

    # Anthropic 은 별도 langchain-anthropic 패키지 사용 — OpenAI 호환 API 가
    # 아닌 자체 chat 프로토콜이라 ChatAnthropic 직접 인스턴스화. 표준 메시지
    # 형식 + native streaming 지원 → disable_streaming 불필요.
    if cfg.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        os.environ.setdefault("ANTHROPIC_API_KEY", cfg.anthropic_api_key)
        log.debug(
            "models.get_model.anthropic",
            tier=tier,
            model=model_name,
            max_tokens=max_tokens,
        )
        # claude-opus-4-7 등 reasoning 모델군은 temperature 를 deprecated
        # ('temperature is deprecated for this model'). 모델명에 'opus' 가
        # 들어간 경우 (또는 향후 reasoning 시리즈) temperature 인자 생략.
        anthropic_kwargs: dict[str, Any] = {
            "model": model_name,
            "api_key": cfg.anthropic_api_key,
            "timeout": cfg.llm_timeout,
            "max_tokens": max_tokens or 4096,
        }
        if "opus" not in model_name.lower():
            anthropic_kwargs["temperature"] = temperature
        _cb = _get_langfuse_callbacks()
        if _cb:
            anthropic_kwargs["callbacks"] = _cb
        instance = ChatAnthropic(**anthropic_kwargs)
        _model_instance_cache[cache_key] = instance
        return instance

    if cfg.provider == "dashscope":
        os.environ.setdefault("DASHSCOPE_API_KEY", cfg.dashscope_api_key)
        api_key = cfg.dashscope_api_key
        base_url = cfg.dashscope_base_url
        # qwen3.5-* / qwen3.6-* 는 dual-mode — default 가 thinking on. fast/default
        # tier 는 즉답성·비용 모두 thinking off 이 유리. probe (2026-04-26)에서
        # "hi" 한 마디에 qwen3.5-flash 가 539 reasoning_tokens 소모 확인.
        # reasoning/strong tier 는 thinking 유지 (planner/coder 추론 가치).
        #
        # Qwen 의 enable_thinking 은 비표준 OpenAI 파라미터 → openai SDK 의
        # ``extra_body`` channel 로 전달. langchain ``model_kwargs`` 는 1.2.x
        # 에서 비표준 키를 reject 하여 ledger/verifier(fast) 가 3ms 만에
        # FAILED 회귀 (v19 관찰, 2026-04-26).
        if tier in ("fast", "default") and any(
            p in model_name.lower() for p in ("qwen3.5", "qwen3.6")
        ):
            extra_kwargs["extra_body"] = {"enable_thinking": False}
    elif cfg.provider == "deepseek":
        os.environ.setdefault("DEEPSEEK_API_KEY", cfg.deepseek_api_key)
        api_key = cfg.deepseek_api_key
        base_url = cfg.deepseek_base_url
        # deepseek-v4 thinking-mode 의 reasoning_content 가 langchain-openai
        # 1.2.x 의 ``_convert_chunk_to_generation_chunk`` 에서 *완전 무시* 됨
        # → streaming 경로로 가면 AIMessage.additional_kwargs 에 보존 못 함
        # → 다음 turn 에서 deepseek 가 400. 비-streaming 경로 (_create_chat_result,
        # 우리 llm_compat patch 적용) 로 강제.
        # ``disable_streaming=True`` 는 langchain-core 의 표준 옵션.
        extra_kwargs["disable_streaming"] = True
    elif cfg.provider == "zai":
        os.environ.setdefault("ZAI_API_KEY", cfg.zai_api_key)
        api_key = cfg.zai_api_key
        base_url = cfg.zai_base_url
        # GLM 4.5/4.6/5.1 도 reasoning_content 반환 — deepseek 와 동일 워크어라운드.
        extra_kwargs["disable_streaming"] = True
    else:
        os.environ.setdefault("OPENROUTER_API_KEY", cfg.openrouter_api_key)
        api_key = cfg.openrouter_api_key
        base_url = "https://openrouter.ai/api/v1"

    log.debug(
        "models.get_model",
        tier=tier,
        model=model_name,
        provider=cfg.provider,
        max_tokens=max_tokens,
    )

    _cb = _get_langfuse_callbacks()
    if _cb:
        extra_kwargs["callbacks"] = _cb
    instance = ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        timeout=cfg.llm_timeout,
        max_tokens=max_tokens,
        **extra_kwargs,
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
