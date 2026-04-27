"""환경변수 기반 설정 관리."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# .env 로드 (프로젝트 루트 또는 Docker /app/.env)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
# Docker 내부에서는 /app/.env도 시도
if (Path("/app/.env")).exists():
    load_dotenv(Path("/app/.env"), override=False)


# ── Portal observability env shim ──────────────────────────────────────────
# The corporate portal (samsungsdscoe.com) injects ``AGENT_OBSERVABILITY_*``
# env vars at pod-start, while every Langfuse client in this repo (and in
# minyoung-mah) reads ``LANGFUSE_*``. Map the portal-side names to the
# canonical Langfuse names *only* when the corresponding LANGFUSE_* slot
# is empty — never override values explicitly set by .env / docker run.
#
# 사내 포털(samsungsdscoe.com) 은 Pod 기동 시 ``AGENT_OBSERVABILITY_*`` env 를
# 주입하지만 이 repo / minyoung-mah 의 Langfuse 클라이언트는 모두
# ``LANGFUSE_*`` 를 읽는다. 대응하는 LANGFUSE_* 가 *비어 있을 때만* 이름을
# 옮긴다 — 명시 설정(.env/docker run -e) 은 절대 덮지 않음.
_AGENT_OBS_LANGFUSE_MAP = (
    ("AGENT_OBSERVABILITY_PROJECT_KEY", "LANGFUSE_PUBLIC_KEY"),
    ("AGENT_OBSERVABILITY_SECRET_KEY", "LANGFUSE_SECRET_KEY"),
    ("AGENT_OBSERVABILITY_BASE_URL", "LANGFUSE_HOST"),
)


def _apply_agent_observability_mapping() -> None:
    """Mirror ``AGENT_OBSERVABILITY_*`` env into ``LANGFUSE_*`` if unset.

    Idempotent — safe to call multiple times. Never overrides an existing
    LANGFUSE_* value (so user-supplied .env wins). No-op when neither side
    has anything.

    ``AGENT_OBSERVABILITY_*`` env 를 ``LANGFUSE_*`` 로 미러링한다(비어 있을
    때만). 멱등 — 여러 번 호출 안전. 기존 LANGFUSE_* 값은 절대 덮지 않으며
    (사용자 .env 우선), 양쪽 모두 비어 있으면 아무 일도 안 한다.
    """
    for src, dst in _AGENT_OBS_LANGFUSE_MAP:
        src_val = os.environ.get(src)
        if src_val and not os.environ.get(dst):
            os.environ[dst] = src_val


# Apply at module import — both CLI and FastAPI daemon entry points read
# Config after import, so by the time get_config() runs the LANGFUSE_*
# slots are filled when the portal injected the AGENT_OBSERVABILITY_* set.
# 모듈 import 시점에 매핑 적용 — CLI/daemon 양쪽 진입점 모두 import 이후
# Config 를 읽으므로 포털 주입 케이스에서 LANGFUSE_* 가 올바로 채워진다.
_apply_agent_observability_mapping()


@dataclass(frozen=True)
class ModelTier:
    """4-Tier 모델 설정."""

    reasoning: str  # 계획, 아키텍처
    strong: str  # 코드 생성, 도구 호출
    default: str  # 분석, 검증
    fast: str  # 파싱, 분류, 메모리 추출


# 프로바이더별 기본 모델
_DASHSCOPE_MODELS = ModelTier(
    reasoning="dashscope/qwen3-max",
    strong="dashscope/qwen3-coder-next",
    default="dashscope/qwen3.5-plus",
    fast="dashscope/qwen3.5-flash",
)

_OPENROUTER_MODELS = ModelTier(
    reasoning="openrouter/qwen/qwen3-max",
    strong="openrouter/z-ai/glm-5.1",
    default="openrouter/qwen/qwen3-coder-next",
    fast="openrouter/qwen/qwen3.5-flash-02-23",
)

# Deepseek V4 시리즈 — 두 모델만 (pro/flash). pro 가 깊은 추론·복잡 코드,
# flash 가 빠른 분석·메모리 추출. tier 별 매핑은 ax 4-tier 의도에 맞춤.
_DEEPSEEK_MODELS = ModelTier(
    reasoning="deepseek-v4-pro",   # planner / critic — 깊이 우선
    strong="deepseek-v4-pro",      # coder / fixer — tool calling + 복잡 코드 생성
    default="deepseek-v4-flash",   # reviewer / researcher — 빠른 분석
    fast="deepseek-v4-flash",      # memory extractor / classifier
)

# Anthropic Claude — Opus 4.7 (reasoning), Sonnet 4.6 (strong/default),
# Haiku 4.5 (fast). 표준 OpenAI 호환성 우수 (deepseek 의 reasoning_content
# 같은 비대칭 contract 없음 — streaming 그대로 사용 가능).
_ANTHROPIC_MODELS = ModelTier(
    reasoning="claude-opus-4-7",         # planner / critic — 가장 깊은 추론
    strong="claude-sonnet-4-6",          # coder / fixer — tool calling + 코드 생성
    default="claude-sonnet-4-6",         # reviewer / researcher
    fast="claude-haiku-4-5",             # memory extractor / classifier
)

# z.ai GLM — 직접 호출 (OpenAI 호환). reasoning_content 반환하는 thinking 모델.
# coding 엔드포인트는 별도 base_url. 5.1-coding 은 무거우니 strong 만 사용.
_ZAI_MODELS = ModelTier(
    reasoning="glm-5.1",                 # planner / critic — 일반 5.1 (코딩보다 빠름)
    strong="glm-5.1",                    # coder / fixer — 일반 5.1
    default="glm-4.6",                   # reviewer — 안정·빠름
    fast="glm-4.5-air",                  # memory extractor — 경량
)

# 추가 프로바이더 프리셋 (GLM, Nemotron 등 — .env에서 모델명 오버라이드로 사용)
# 예: STRONG_MODEL=openrouter/z-ai/glm-5.1
#     DEFAULT_MODEL=openrouter/nvidia/nemotron-3-super-120b-a12b
#     FAST_MODEL=openrouter/qwen/qwen3.5-35b-a3b
# 이 모델들은 native tool calling 미지원 시 프롬프트 기반 폴백이 자동 적용됨


@dataclass
class Config:
    """전역 설정."""

    # 프로바이더
    provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "openrouter"))

    # API 키
    dashscope_api_key: str = field(
        default_factory=lambda: os.getenv("DASHSCOPE_API_KEY", "")
    )
    dashscope_base_url: str = field(
        default_factory=lambda: os.getenv(
            "DASHSCOPE_BASE_URL",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        )
    )
    openrouter_api_key: str = field(
        default_factory=lambda: os.getenv("OPENROUTER_API_KEY", "")
    )

    # Deepseek (V4 라인업) 직접 호출 — OpenAI 호환 API
    deepseek_api_key: str = field(
        default_factory=lambda: os.getenv("DEEPSEEK_API_KEY", "")
    )
    deepseek_base_url: str = field(
        default_factory=lambda: os.getenv(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
        )
    )

    # Anthropic (Claude) — 표준 메시지 contract. langchain-anthropic 사용.
    anthropic_api_key: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", "")
    )

    # z.ai (GLM 공식 API) — 직접 호출 (OpenAI 호환).
    # standard endpoint: https://api.z.ai/api/paas/v4
    # coding endpoint:   https://api.z.ai/api/coding/paas/v4 (glm-*-coding 전용)
    zai_api_key: str = field(
        default_factory=lambda: os.getenv("ZAI_API_KEY", "")
    )
    zai_base_url: str = field(
        default_factory=lambda: os.getenv(
            "ZAI_BASE_URL", "https://api.z.ai/api/paas/v4"
        )
    )

    # LiteLLM Proxy (Docker 하니스 모드)
    litellm_proxy_url: str = field(
        default_factory=lambda: os.getenv("LITELLM_PROXY_URL", "")
    )
    litellm_master_key: str = field(
        default_factory=lambda: os.getenv("LITELLM_MASTER_KEY", "")
    )

    # ── Portal LiteLLM gateway (LLM_PROVIDER=litellm_portal) ──
    # 사내 포털(samsungsdscoe.com) 의 LiteLLM 게이트웨이를 OpenAI-호환
    # base_url 로 직접 호출하는 모드. 4-tier 가 모두 같은 모델로 fallback
    # 되도록 의도(KEY 가 sonnet-4-6 하나만 허용하는 dev pod 환경 가정).
    # `LITELLM_MODEL_PREFIX` 가 비어 있으면 prefix 미부착(KEY 가 prefix 없는
    # 모델명만 허용하는 ax dev pod 케이스). apt-legal 처럼 "openai/" 등
    # prefix 가 KEY 에 등록돼 있는 환경이면 명시 설정으로 부착.
    litellm_base_url: str = field(
        default_factory=lambda: os.getenv("LITELLM_BASE_URL", "")
    )
    litellm_api_key: str = field(
        default_factory=lambda: os.getenv("LITELLM_API_KEY", "")
    )
    litellm_model: str = field(
        default_factory=lambda: os.getenv(
            "LITELLM_MODEL", "us.anthropic.claude-sonnet-4-6"
        )
    )

    # Langfuse
    langfuse_public_key: str = field(
        default_factory=lambda: os.getenv("LANGFUSE_PUBLIC_KEY", "")
    )
    langfuse_secret_key: str = field(
        default_factory=lambda: os.getenv("LANGFUSE_SECRET_KEY", "")
    )

    # 에이전트 설정
    max_iterations: int = field(
        default_factory=lambda: int(os.getenv("MAX_ITERATIONS", "150"))
    )
    llm_timeout: int = field(
        default_factory=lambda: int(os.getenv("LLM_TIMEOUT", "60"))
    )
    memory_db_path: str = field(
        default_factory=lambda: os.getenv(
            "MEMORY_DB_PATH",
            # v2 schema (tier/scope) — incompatible with the old layer/project_id
            # DB. Old ``memory.db`` files are left in place; a fresh file is
            # created at ``ax.v2.db``. Per plan Phase 3 migration note.
            str(_PROJECT_ROOT / "memory_store" / "ax.v2.db"),
        )
    )

    # 프로젝트 격리 키 — workspace 경로 해시로 ax-agent.sh 에서 주입됨.
    # 모든 메모리 tier (user/project/domain) 의 scope 로 사용되어 다른
    # 워크스페이스 세션과 DB 를 공유하더라도 교차 오염을 차단한다.
    # 빈 문자열이면 legacy 모드 (세션 간 공유).
    project_id: str = field(
        default_factory=lambda: os.getenv("AX_PROJECT_ID", "")
    )

    # Orchestrator tier — 최상위 ReAct 드라이버가 사용할 model tier.
    # "reasoning" (조율/플래닝 중심, 권장) / "strong" (도구 호출 중심, 기존 동작) /
    # "default" / "fast" 중 하나.
    orchestrator_tier: str = field(
        default_factory=lambda: os.getenv("ORCHESTRATOR_TIER", "reasoning")
    )

    # ── Sufficiency loop (apt-legal 패턴 이식, MAX_ITER=1 보수 default) ──
    # default ON — 별도 설정 없으면 켜진다. LangGraph 종료 직전에
    # rule_gate(test/lint/todo/PRD) → MEDIUM 분기에서 LLM critic 호출.
    # MAX_ITER=1 이라 정상(HIGH) 케이스에서는 critic 호출 자체가 일어나지
    # 않아 비용 영향이 작다. 명시적 비활성: ``AX_SUFFICIENCY_ENABLED=0``.
    sufficiency_enabled: bool = field(
        default_factory=lambda: os.getenv("AX_SUFFICIENCY_ENABLED", "1") == "1"
    )
    sufficiency_max_iterations: int = field(
        default_factory=lambda: int(os.getenv("AX_SUFF_MAX_ITER", "1"))
    )
    sufficiency_high_todo: float = field(
        default_factory=lambda: float(os.getenv("AX_SUFF_HIGH_TODO", "0.9"))
    )
    sufficiency_low_todo: float = field(
        default_factory=lambda: float(os.getenv("AX_SUFF_LOW_TODO", "0.5"))
    )
    sufficiency_high_prd: float = field(
        default_factory=lambda: float(os.getenv("AX_SUFF_HIGH_PRD", "0.85"))
    )
    sufficiency_low_prd: float = field(
        default_factory=lambda: float(os.getenv("AX_SUFF_LOW_PRD", "0.4"))
    )

    # 프로젝트 경로
    project_root: Path = field(default_factory=lambda: _PROJECT_ROOT)

    @property
    def model_tier(self) -> ModelTier:
        """현재 프로바이더에 맞는 모델 티어 반환."""
        # LiteLLM Proxy 모드에서는 티어 이름이 곧 모델 이름
        if self.provider == "litellm" or self.litellm_proxy_url:
            return ModelTier(
                reasoning=os.getenv("REASONING_MODEL", "reasoning"),
                strong=os.getenv("STRONG_MODEL", "strong"),
                default=os.getenv("DEFAULT_MODEL", "default"),
                fast=os.getenv("FAST_MODEL", "fast"),
            )
        # Portal LiteLLM gateway — 4-tier all collapse onto one model.
        # 포털 LiteLLM 게이트웨이 — 4-tier 가 한 모델로 수렴. dev pod 의 KEY 가
        # sonnet-4-6 하나만 허용하는 케이스 기본 동작. tier 별 override 도 지원.
        if self.provider == "litellm_portal":
            base_model = self.litellm_model
            return ModelTier(
                reasoning=os.getenv("REASONING_MODEL", base_model),
                strong=os.getenv("STRONG_MODEL", base_model),
                default=os.getenv("DEFAULT_MODEL", base_model),
                fast=os.getenv("FAST_MODEL", base_model),
            )
        if self.provider == "dashscope":
            base = _DASHSCOPE_MODELS
        elif self.provider == "deepseek":
            base = _DEEPSEEK_MODELS
        elif self.provider == "anthropic":
            base = _ANTHROPIC_MODELS
        elif self.provider == "zai":
            base = _ZAI_MODELS
        else:
            base = _OPENROUTER_MODELS
        return ModelTier(
            reasoning=os.getenv("REASONING_MODEL", base.reasoning),
            strong=os.getenv("STRONG_MODEL", base.strong),
            default=os.getenv("DEFAULT_MODEL", base.default),
            fast=os.getenv("FAST_MODEL", base.fast),
        )

    @property
    def api_key(self) -> str:
        """현재 프로바이더의 API 키."""
        if self.provider == "litellm" or self.litellm_proxy_url:
            return self.litellm_master_key
        if self.provider == "litellm_portal":
            return self.litellm_api_key
        if self.provider == "dashscope":
            return self.dashscope_api_key
        if self.provider == "deepseek":
            return self.deepseek_api_key
        if self.provider == "anthropic":
            return self.anthropic_api_key
        if self.provider == "zai":
            return self.zai_api_key
        return self.openrouter_api_key


# 싱글턴
_config: Config | None = None


def get_config() -> Config:
    """전역 Config 인스턴스 반환."""
    global _config
    if _config is None:
        _config = Config()
    return _config
