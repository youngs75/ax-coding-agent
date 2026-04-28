"""Provider routing regression tests for ``coding_agent.models.get_model``.

Background — 2026-04-28 portal e2e regression:

``models.py`` had branches for ``anthropic`` / ``dashscope`` / ``deepseek`` /
``zai`` / openrouter (else), but **no branch for ``litellm_portal``**.  The
provider therefore fell through to the openrouter else, instantiating
``ChatOpenAI`` with ``base_url='https://openrouter.ai/api/v1'`` and an empty
key — leading to ``401 No cookie auth credentials found`` on the first call.
``config.py`` exposed ``litellm_base_url`` / ``litellm_api_key`` for the
provider, but the model-instantiation site never read them.

This module locks down provider→base_url routing so future renames or refactors
can't reintroduce the silent fallback.

배경 — 2026-04-28 포털 e2e 회귀:
``models.py`` 의 provider 분기에서 ``litellm_portal`` 만 누락되어 else 의
openrouter 로 silent fallback. 401 발생. config 에는 portal field 가 있었으나
모델 인스턴스화 path 가 그것을 안 읽었던 것이 진짜 원인. 본 테스트가 그
회귀 형태를 영구 차단.
"""

from __future__ import annotations

import pytest

from coding_agent import config, models


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    """Reset module-level singletons so each test sees a fresh Config / cache.

    각 테스트가 독립된 Config / 모델 캐시 상태에서 시작하도록 module-level
    싱글턴 초기화.
    """
    config._config = None
    models._model_instance_cache.clear()
    yield
    config._config = None
    models._model_instance_cache.clear()


def test_litellm_portal_routes_to_portal_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """``LLM_PROVIDER=litellm_portal`` must instantiate against the portal LiteLLM.

    ``litellm_portal`` provider 시 portal LiteLLM 게이트웨이로 라우팅돼야 함.
    분기 누락 시 openrouter 로 fallback 되어 401 (2026-04-28 회귀).
    """
    monkeypatch.setenv("LLM_PROVIDER", "litellm_portal")
    monkeypatch.setenv("LITELLM_BASE_URL", "https://litellm.example.com")
    monkeypatch.setenv("LITELLM_API_KEY", "test-portal-key")
    monkeypatch.setenv("FAST_MODEL", "us.anthropic.claude-sonnet-4-6")
    # Avoid leaking the docker-harness LITELLM_PROXY_URL path.
    monkeypatch.delenv("LITELLM_PROXY_URL", raising=False)

    instance = models.get_model("fast")

    base_url = str(instance.openai_api_base)
    assert "litellm.example.com" in base_url
    assert "openrouter" not in base_url
    assert instance.openai_api_key.get_secret_value() == "test-portal-key"
    assert instance.model_name == "us.anthropic.claude-sonnet-4-6"


def test_default_provider_falls_back_to_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``LLM_PROVIDER``, model goes to openrouter (existing behaviour).

    LLM_PROVIDER 미설정 시 openrouter 로 가는 기존 동작이 보존되는지.
    portal 분기 추가 회귀 방지.
    """
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LITELLM_PROXY_URL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-or-key")

    instance = models.get_model("fast")

    assert "openrouter.ai" in str(instance.openai_api_base)


def test_litellm_portal_uses_default_base_url_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Portal provider with no ``LITELLM_BASE_URL`` falls back to the SDS default.

    ``LITELLM_BASE_URL`` ENV 가 빠져도 config 의 ``DEFAULT_LITELLM_BASE_URL``
    (사내 portal endpoint) 로 라우팅돼야 한다.
    """
    monkeypatch.setenv("LLM_PROVIDER", "litellm_portal")
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
    monkeypatch.delenv("LITELLM_PROXY_URL", raising=False)
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    monkeypatch.setenv("FAST_MODEL", "us.anthropic.claude-sonnet-4-6")

    instance = models.get_model("fast")

    base_url = str(instance.openai_api_base)
    assert "samsungsdscoe.com" in base_url
    assert "openrouter" not in base_url
