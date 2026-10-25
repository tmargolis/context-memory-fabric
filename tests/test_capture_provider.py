"""The providers unattended capture workers extract with
(server.adapters.capture_provider).

Since v0.1.2 capture shares the ranked extraction list; an older install's
CMF_CAPTURE_LLM_PROVIDER still wins on its own. The cases that matter are the
ones that would silently spend a metered API's quota: the old setting must
keep winning over a ranked list (the v0.1.1 bug), a typo must fail before any
extraction call, and the switch must never leak past the worker's pass.
"""

import os

import pytest

from server.adapters.capture_provider import capture_llm_provider
from server.core.config import (
    LOCAL_PROVIDER,
    _llm_providers,
    capture_llm_override,
    capture_llm_provider_from_env,
    capture_llm_providers_from_env,
)


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("CMF_CAPTURE_LLM_PROVIDER", "CMF_LLM_PROVIDER", "CMF_LLM_PROVIDERS"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_shares_the_ranked_list(clean_env):
    clean_env.setenv("CMF_LLM_PROVIDERS", "local,anthropic")
    clean_env.setenv("CMF_LLM_PROVIDER", "local")
    assert capture_llm_providers_from_env() == ("local", "anthropic")
    assert not capture_llm_override()


def test_shares_the_single_provider(clean_env):
    clean_env.setenv("CMF_LLM_PROVIDER", "openai")
    assert capture_llm_providers_from_env() == ("openai",)


def test_blank_override_means_shared(clean_env):
    clean_env.setenv("CMF_CAPTURE_LLM_PROVIDER", "  ")
    clean_env.setenv("CMF_LLM_PROVIDER", "local")
    assert capture_llm_provider_from_env() == LOCAL_PROVIDER
    assert not capture_llm_override()


def test_older_override_is_normalised_and_wins_alone(clean_env):
    clean_env.setenv("CMF_LLM_PROVIDERS", "anthropic,openai")
    clean_env.setenv("CMF_CAPTURE_LLM_PROVIDER", "  LOCAL ")
    assert capture_llm_providers_from_env() == (LOCAL_PROVIDER,)
    assert capture_llm_override()


def test_typo_raises(clean_env):
    clean_env.setenv("CMF_CAPTURE_LLM_PROVIDER", "lcoal")
    with pytest.raises(ValueError, match="CMF_CAPTURE_LLM_PROVIDER"):
        capture_llm_provider_from_env()


def test_override_beats_a_ranked_list_inside_the_pass(clean_env):
    """v0.1.1 switched only CMF_LLM_PROVIDER, so a ranked list stayed in charge
    and capture ran on metered APIs despite CMF_CAPTURE_LLM_PROVIDER=local."""
    clean_env.setenv("CMF_LLM_PROVIDERS", "anthropic,openai")
    clean_env.setenv("CMF_LLM_PROVIDER", "anthropic")
    clean_env.setenv("CMF_CAPTURE_LLM_PROVIDER", "local")
    with capture_llm_provider() as provider:
        assert provider == LOCAL_PROVIDER
        assert _llm_providers() == (LOCAL_PROVIDER,)
    assert os.environ["CMF_LLM_PROVIDERS"] == "anthropic,openai"
    assert os.environ["CMF_LLM_PROVIDER"] == "anthropic"


def test_shared_list_keeps_its_fallbacks_inside_the_pass(clean_env):
    clean_env.setenv("CMF_LLM_PROVIDERS", "local,gemini")
    clean_env.setenv("CMF_LLM_PROVIDER", "local")
    with capture_llm_provider() as provider:
        assert provider == LOCAL_PROVIDER
        assert _llm_providers() == ("local", "gemini")


def test_restores_unset_providers(clean_env):
    clean_env.setenv("CMF_CAPTURE_LLM_PROVIDER", "local")
    with capture_llm_provider():
        assert os.environ["CMF_LLM_PROVIDER"] == LOCAL_PROVIDER
    assert "CMF_LLM_PROVIDER" not in os.environ
    assert "CMF_LLM_PROVIDERS" not in os.environ


def test_restores_after_exception(clean_env):
    clean_env.setenv("CMF_LLM_PROVIDER", "gemini")
    clean_env.setenv("CMF_CAPTURE_LLM_PROVIDER", "local")
    with pytest.raises(RuntimeError):
        with capture_llm_provider():
            raise RuntimeError("extraction failed")
    assert os.environ["CMF_LLM_PROVIDER"] == "gemini"
    assert "CMF_LLM_PROVIDERS" not in os.environ


def test_typo_raises_before_touching_provider(clean_env):
    clean_env.setenv("CMF_LLM_PROVIDER", "gemini")
    clean_env.setenv("CMF_CAPTURE_LLM_PROVIDER", "lcoal")
    with pytest.raises(ValueError):
        with capture_llm_provider():
            pytest.fail("block must not run")
    assert os.environ["CMF_LLM_PROVIDER"] == "gemini"
