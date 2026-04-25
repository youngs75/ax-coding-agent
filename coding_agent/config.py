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

    # LiteLLM Proxy (Docker 하니스 모드)
    litellm_proxy_url: str = field(
        default_factory=lambda: os.getenv("LITELLM_PROXY_URL", "")
    )
    litellm_master_key: str = field(
        default_factory=lambda: os.getenv("LITELLM_MASTER_KEY", "")
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
        base = (
            _DASHSCOPE_MODELS if self.provider == "dashscope" else _OPENROUTER_MODELS
        )
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
        if self.provider == "dashscope":
            return self.dashscope_api_key
        return self.openrouter_api_key


# 싱글턴
_config: Config | None = None


def get_config() -> Config:
    """전역 Config 인스턴스 반환."""
    global _config
    if _config is None:
        _config = Config()
    return _config
