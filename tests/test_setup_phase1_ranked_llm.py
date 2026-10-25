"""Setup simplification Phase 1, item 1.8: ranked LLM providers
(docs/setup-simplification-plan.md). Provider SDK errors are built from the
real exception classes; no live calls."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import openai
import pytest

from server.core import startup_check
from server.core.config import load_config
from server.core.rate_limiter import GeminiQuotaExhaustedError
from server.providers import memory_graphiti
from server.providers.failover_llm import FailoverLLMClient, should_fail_over


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("CMF_LLM_PROVIDERS", "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CMF_LLM_PROVIDER", "local")
    monkeypatch.setenv("CMF_EMBED_PROVIDER", "local")
    monkeypatch.setenv("FALKORDB_DATABASE", "CMF-local")


def _http_error(cls, status, url="https://api.anthropic.com/v1/messages", message="boom"):
    return cls(message, response=httpx.Response(status, request=httpx.Request("POST", url)), body=None)


# --- config ----------------------------------------------------------------------------------------


def test_list_wins_and_alias_still_works(monkeypatch):
    assert load_config().llm_providers == ("local",)
    monkeypatch.setenv("CMF_LLM_PROVIDERS", " Anthropic,openai,,anthropic ")
    config = load_config()
    assert config.llm_provider == "anthropic" and config.llm_providers == ("anthropic", "openai")


def test_unknown_provider_in_list(monkeypatch):
    monkeypatch.setenv("CMF_LLM_PROVIDERS", "anthropic,claude")
    with pytest.raises(ValueError, match="'claude' is not a recognised provider"):
        load_config()


# --- which errors fail over ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc, kind",
    [
        (_http_error(anthropic.RateLimitError, 429, message="rate_limit_error"), "provider_quota"),
        (_http_error(anthropic.AuthenticationError, 401, message="invalid x-api-key"), "provider_auth"),
        (_http_error(openai.RateLimitError, 429, url="https://api.openai.com/v1/chat", message="insufficient_quota"), None),
        (GeminiQuotaExhaustedError("no headroom"), "provider_quota"),
    ],
)
def test_should_fail_over(exc, kind):
    got = should_fail_over(exc)
    if kind is None:
        assert got in ("provider_billing", "provider_quota")  # either way it fails over
    else:
        assert got == kind


def test_setup_mistakes_do_not_fail_over():
    assert should_fail_over(httpx.ConnectError("[Errno 61] Connection refused")) is None
    assert should_fail_over(ValueError("schema mismatch")) is None


# --- FailoverLLMClient ------------------------------------------------------------------------------


def _client(name, result=None, error=None):
    c = MagicMock()
    c.config, c.model, c.small_model, c.temperature, c.max_tokens = MagicMock(), name, name, 0, 1000
    c.generate_response = AsyncMock(return_value=result, side_effect=error)
    return c


def test_first_provider_serves():
    a, b = _client("a", {"ok": "a"}), _client("b", {"ok": "b"})
    client = FailoverLLMClient([("anthropic", a), ("openai", b)])
    assert asyncio.run(client.generate_response(["m"])) == {"ok": "a"}
    assert client.last_served_by == "anthropic" and not b.generate_response.called


def test_rate_limit_moves_to_next_and_logs(caplog):
    a = _client("a", error=_http_error(anthropic.RateLimitError, 429, message="rate_limit_error"))
    b = _client("b", {"ok": "b"})
    client = FailoverLLMClient([("anthropic", a), ("openai", b)])
    with caplog.at_level("INFO"):
        assert asyncio.run(client.generate_response(["m"], response_model=None)) == {"ok": "b"}
    b.generate_response.assert_awaited_once_with(["m"], response_model=None)
    assert client.last_served_by == "openai"
    assert "LLM call served by openai after anthropic (provider_quota)" in caplog.text


def test_unreachable_local_server_raises_instead_of_paying():
    a = _client("a", error=httpx.ConnectError("Connection refused"))
    b = _client("b", {"ok": "b"})
    with pytest.raises(httpx.ConnectError):
        asyncio.run(FailoverLLMClient([("local", a), ("anthropic", b)]).generate_response(["m"]))
    assert not b.generate_response.called


def test_last_provider_error_is_raised():
    err = _http_error(anthropic.RateLimitError, 429, message="rate_limit_error")
    a, b = _client("a", error=err), _client("b", error=err)
    with pytest.raises(anthropic.RateLimitError):
        asyncio.run(FailoverLLMClient([("anthropic", a), ("anthropic", b)]).generate_response(["m"]))


def test_tracer_reaches_every_client():
    a, b = _client("a"), _client("b")
    FailoverLLMClient([("x", a), ("y", b)]).set_tracer("T")
    a.set_tracer.assert_called_once_with("T") and b.set_tracer.assert_called_once_with("T")


# --- construction -----------------------------------------------------------------------------------


def test_single_provider_builds_the_plain_client():
    client = memory_graphiti._build_llm_client(load_config(), "m")
    assert not isinstance(client, FailoverLLMClient)


def test_chain_builds_failover_with_each_providers_model(monkeypatch):
    monkeypatch.setenv("CMF_LLM_PROVIDERS", "anthropic,openai,local")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    client = memory_graphiti._build_llm_client(load_config(), "claude-sonnet-5-5")
    assert isinstance(client, FailoverLLMClient) and client.providers == ["anthropic", "openai", "local"]
    assert [c.model for _, c in client.clients] == ["claude-sonnet-5-5", "gpt-5.5", load_config().local_llm_model]


def test_fallback_without_key_is_skipped_primary_without_key_raises(monkeypatch, caplog):
    monkeypatch.setenv("CMF_LLM_PROVIDERS", "local,openai")
    client = memory_graphiti._build_llm_client(load_config(), "m")
    assert not isinstance(client, FailoverLLMClient)  # only local is usable
    assert "LLM fallback openai skipped" in caplog.text
    monkeypatch.setenv("CMF_LLM_PROVIDERS", "openai,local")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        memory_graphiti._build_llm_client(load_config(), "m")


def test_gemini_quota_skips_to_next_provider(monkeypatch):
    monkeypatch.setenv("CMF_LLM_PROVIDERS", "gemini,anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    limiter = MagicMock(chain=["gemini-3.5-flash-lite"])
    limiter.reserve.side_effect = GeminiQuotaExhaustedError("used up")
    with patch.object(memory_graphiti, "get_default_rate_limiter", return_value=limiter), \
         patch.object(memory_graphiti, "get_graphiti", return_value="G") as get:
        graphiti, model = memory_graphiti.get_graphiti_for_operation()
    assert (graphiti, model) == ("G", "claude-sonnet-5-5")
    assert get.call_args.kwargs["skip_primary"] is True


def test_gemini_quota_without_fallback_still_raises(monkeypatch):
    monkeypatch.setenv("CMF_LLM_PROVIDER", "gemini")
    limiter = MagicMock(chain=["gemini-3.5-flash-lite"])
    limiter.reserve.side_effect = GeminiQuotaExhaustedError("used up")
    with patch.object(memory_graphiti, "get_default_rate_limiter", return_value=limiter):
        with pytest.raises(GeminiQuotaExhaustedError):
            memory_graphiti.get_graphiti_for_operation()


# --- startup check and provider tests -------------------------------------------------------------


def test_fallback_key_missing_is_a_warning(monkeypatch):
    monkeypatch.setenv("CMF_LLM_PROVIDERS", "local,anthropic")
    with patch.object(startup_check, "_wait_for_falkordb", return_value=(None, "skip")):
        result = startup_check.check(falkordb_wait=0)
    assert result.problems == ["skip"]
    assert any("LLM fallback provider is anthropic" in w and "skipped" in w for w in result.warnings)


def test_provider_tests_cover_the_chain(monkeypatch):
    monkeypatch.setenv("CMF_LLM_PROVIDERS", "local,anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    from types import SimpleNamespace

    tests = startup_check.provider_tests(lambda u, h: SimpleNamespace(status_code=200, json=lambda: {"data": []}))
    assert [(t["role"], t["provider"]) for t in tests] == [
        ("LLM", "local"), ("LLM fallback", "anthropic"), ("embeddings", "local"), ("background capture", "local")]
