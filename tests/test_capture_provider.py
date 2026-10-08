"""CMF_CAPTURE_LLM_PROVIDER: the provider unattended capture workers extract
with (server.adapters.capture_provider).

The cases that matter are the ones that would silently spend a metered
API's quota: an unset setting must stay local, a typo must fail before any
extraction call, and the switch must never leak past the worker's pass.
"""

import os

import pytest

from server.adapters.capture_provider import capture_llm_provider
from server.core.config import LOCAL_PROVIDER, capture_llm_provider_from_env


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("CMF_CAPTURE_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("CMF_LLM_PROVIDER", raising=False)
    return monkeypatch


def test_unset_defaults_to_local(clean_env):
    assert capture_llm_provider_from_env() == LOCAL_PROVIDER


def test_blank_defaults_to_local(clean_env):
    clean_env.setenv("CMF_CAPTURE_LLM_PROVIDER", "  ")
    assert capture_llm_provider_from_env() == LOCAL_PROVIDER


def test_override_is_normalised(clean_env):
    clean_env.setenv("CMF_CAPTURE_LLM_PROVIDER", "  GEMINI ")
    assert capture_llm_provider_from_env() == "gemini"


def test_typo_raises(clean_env):
    clean_env.setenv("CMF_CAPTURE_LLM_PROVIDER", "lcoal")
    with pytest.raises(ValueError, match="CMF_CAPTURE_LLM_PROVIDER"):
        capture_llm_provider_from_env()


def test_forces_local_over_interactive_provider_by_default(clean_env):
    clean_env.setenv("CMF_LLM_PROVIDER", "gemini")
    with capture_llm_provider() as provider:
        assert provider == LOCAL_PROVIDER
        assert os.environ["CMF_LLM_PROVIDER"] == LOCAL_PROVIDER
    assert os.environ["CMF_LLM_PROVIDER"] == "gemini"


def test_uses_override_inside_block(clean_env):
    clean_env.setenv("CMF_LLM_PROVIDER", "local")
    clean_env.setenv("CMF_CAPTURE_LLM_PROVIDER", "gemini")
    with capture_llm_provider():
        assert os.environ["CMF_LLM_PROVIDER"] == "gemini"
    assert os.environ["CMF_LLM_PROVIDER"] == "local"


def test_restores_unset_provider(clean_env):
    with capture_llm_provider():
        assert os.environ["CMF_LLM_PROVIDER"] == LOCAL_PROVIDER
    assert "CMF_LLM_PROVIDER" not in os.environ


def test_restores_after_exception(clean_env):
    clean_env.setenv("CMF_LLM_PROVIDER", "gemini")
    with pytest.raises(RuntimeError):
        with capture_llm_provider():
            raise RuntimeError("extraction failed")
    assert os.environ["CMF_LLM_PROVIDER"] == "gemini"


def test_typo_raises_before_touching_provider(clean_env):
    clean_env.setenv("CMF_LLM_PROVIDER", "gemini")
    clean_env.setenv("CMF_CAPTURE_LLM_PROVIDER", "lcoal")
    with pytest.raises(ValueError):
        with capture_llm_provider():
            pytest.fail("block must not run")
    assert os.environ["CMF_LLM_PROVIDER"] == "gemini"
