"""MS10a: anthropic and openai providers, and per-role local endpoints.

No network: request shapes are checked on the kwargs CMF builds, and client
selection on the objects the builders return. The live round-trip is
acceptance tests 1-2 in docs/plan-active.md, run with real keys.
"""

import asyncio
from types import SimpleNamespace

import pytest

from server.core import rate_limiter
from server.core.config import DEFAULT_LOCAL_BASE_URL, load_config

ENV_VARS = (
    "CMF_LLM_PROVIDER", "CMF_EMBED_PROVIDER", "CMF_CAPTURE_LLM_PROVIDER",
    "CMF_LOCAL_BASE_URL", "CMF_LOCAL_API_KEY",
    "CMF_LOCAL_LLM_BASE_URL", "CMF_LOCAL_LLM_API_KEY", "CMF_LOCAL_EMBED_BASE_URL", "CMF_LOCAL_EMBED_API_KEY",
    "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "CMF_ANTHROPIC_MODEL", "CMF_OPENAI_MODEL", "CMF_OPENAI_EMBED_MODEL", "CMF_ANTHROPIC_EFFORT",
    "FALKORDB_DATABASE", "EMBEDDING_DIM", "CMF_RERANKER",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Isolated from the developer's .env (see tests/test_spark_config.py)."""
    monkeypatch.setattr("server.core.config.load_dotenv", lambda *a, **k: None)
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FALKORDB_DATABASE", "test-graph")
    rate_limiter.reset_default_rate_limiter()
    yield monkeypatch
    rate_limiter.reset_default_rate_limiter()


def _hosted(env, llm="anthropic", embed="openai"):
    env.setenv("CMF_LLM_PROVIDER", llm)
    env.setenv("CMF_EMBED_PROVIDER", embed)
    env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    env.setenv("OPENAI_API_KEY", "sk-openai-test")


# --- config ------------------------------------------------------------------


def test_hosted_providers_are_valid_and_default_models(clean_env):
    _hosted(clean_env)
    config = load_config()
    assert (config.llm_provider, config.embed_provider) == ("anthropic", "openai")
    assert config.anthropic_model == "claude-sonnet-5-5"
    assert config.openai_model == "gpt-5.5"
    assert config.openai_embed_model == "text-embedding-3-small"
    assert config.anthropic_effort is None
    assert config.memory_enabled is True


def test_model_and_effort_overrides(clean_env):
    _hosted(clean_env)
    clean_env.setenv("CMF_ANTHROPIC_MODEL", "claude-opus-5-5")
    clean_env.setenv("CMF_ANTHROPIC_EFFORT", " Low ")
    config = load_config()
    assert config.anthropic_model == "claude-opus-5-5"
    assert config.anthropic_effort == "low"


def test_bad_effort_raises(clean_env):
    clean_env.setenv("CMF_ANTHROPIC_EFFORT", "turbo")
    with pytest.raises(ValueError, match="CMF_ANTHROPIC_EFFORT"):
        load_config()


def test_anthropic_cannot_embed(clean_env):
    clean_env.setenv("CMF_EMBED_PROVIDER", "anthropic")
    with pytest.raises(ValueError, match="no embeddings API"):
        load_config()


def test_capture_provider_accepts_hosted(clean_env):
    from server.core.config import capture_llm_provider_from_env

    clean_env.setenv("CMF_CAPTURE_LLM_PROVIDER", "openai")
    assert capture_llm_provider_from_env() == "openai"


@pytest.mark.parametrize(
    "missing, enabled",
    [(None, True), ("ANTHROPIC_API_KEY", False), ("OPENAI_API_KEY", False)],
)
def test_memory_enabled_needs_each_roles_key(clean_env, missing, enabled):
    _hosted(clean_env)
    if missing:
        clean_env.delenv(missing)
    assert load_config().memory_enabled is enabled


def test_no_gemini_key_needed_for_hosted(clean_env):
    _hosted(clean_env)
    assert load_config().gemini_api_key is None
    assert load_config().memory_enabled is True


def test_local_urls_fall_back_to_shared_setting(clean_env):
    clean_env.setenv("CMF_LOCAL_BASE_URL", "http://127.0.0.1:9999/v1")
    clean_env.setenv("CMF_LOCAL_API_KEY", "shared")
    config = load_config()
    assert config.local_llm_base_url == config.local_embed_base_url == "http://127.0.0.1:9999/v1"
    assert config.local_llm_api_key == config.local_embed_api_key == "shared"


def test_local_urls_split_per_role(clean_env):
    clean_env.setenv("CMF_LOCAL_LLM_BASE_URL", "http://127.0.0.1:12345/v1")
    clean_env.setenv("CMF_LOCAL_EMBED_BASE_URL", "http://127.0.0.1:11434/v1")
    clean_env.setenv("CMF_LOCAL_EMBED_API_KEY", "ollama")
    config = load_config()
    assert config.local_llm_base_url == "http://127.0.0.1:12345/v1"
    assert config.local_embed_base_url == "http://127.0.0.1:11434/v1"
    assert config.local_embed_api_key == "ollama"
    assert config.local_base_url == DEFAULT_LOCAL_BASE_URL


def test_hosted_llm_with_local_embedder(clean_env):
    """A frontier LLM with a laptop embedder: only the embed URL matters."""
    clean_env.setenv("CMF_LLM_PROVIDER", "anthropic")
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    clean_env.setenv("CMF_EMBED_PROVIDER", "local")
    clean_env.setenv("CMF_LOCAL_EMBED_BASE_URL", "http://127.0.0.1:11434/v1")
    clean_env.setenv("CMF_LOCAL_BASE_URL", " ")
    assert load_config().memory_enabled is True
    clean_env.setenv("CMF_LOCAL_EMBED_BASE_URL", " ")
    assert load_config().memory_enabled is False


# --- Graphiti builders ---------------------------------------------------------


def test_builders_on_anthropic_and_openai(clean_env):
    from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
    from graphiti_core.llm_client.openai_client import OpenAIClient

    from server.providers import memory_graphiti as mg
    from server.providers.anthropic_client import StructuredAnthropicClient
    from server.providers.openai_embedder import DimensionedOpenAIEmbedder

    _hosted(clean_env)
    clean_env.setenv("EMBEDDING_DIM", "768")
    clean_env.setenv("CMF_ANTHROPIC_EFFORT", "low")
    config = load_config()
    model = mg.resolve_llm_model(config)
    assert model == "claude-sonnet-5-5"

    llm = mg._build_llm_client(config, model)
    assert isinstance(llm, StructuredAnthropicClient)
    assert (llm.model, llm.effort) == ("claude-sonnet-5-5", "low")

    embedder = mg._build_embedder(config)
    assert isinstance(embedder, DimensionedOpenAIEmbedder)
    assert embedder.config.embedding_dim == 768
    assert embedder.config.embedding_model == "text-embedding-3-small"

    # Claude returns no logprobs, so the anthropic path reranks with CMF's own.
    assert not isinstance(mg._build_cross_encoder(config, model), OpenAIRerankerClient)

    clean_env.setenv("CMF_LLM_PROVIDER", "openai")
    config = load_config()
    model = mg.resolve_llm_model(config)
    assert model == "gpt-5.5"
    assert isinstance(mg._build_llm_client(config, model), OpenAIClient)
    assert isinstance(mg._build_cross_encoder(config, model), OpenAIRerankerClient)


def test_builder_names_the_missing_key(clean_env):
    from server.providers import memory_graphiti as mg

    clean_env.setenv("CMF_LLM_PROVIDER", "anthropic")
    config = load_config()
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        mg._build_llm_client(config, "claude-opus-5-5")


def test_local_builders_use_per_role_urls(clean_env):
    from server.providers import memory_graphiti as mg

    clean_env.setenv("CMF_LLM_PROVIDER", "local")
    clean_env.setenv("CMF_EMBED_PROVIDER", "local")
    clean_env.setenv("CMF_LOCAL_LLM_BASE_URL", "http://127.0.0.1:12345/v1")
    clean_env.setenv("CMF_LOCAL_EMBED_BASE_URL", "http://127.0.0.1:11434/v1")
    config = load_config()
    assert mg._build_llm_client(config, "m").config.base_url == "http://127.0.0.1:12345/v1"
    assert mg._build_embedder(config).config.base_url == "http://127.0.0.1:11434/v1"


# --- Anthropic request shape ---------------------------------------------------


def test_request_has_schema_and_none_of_the_rejected_fields():
    from server.policies.reasoning_episode import _EPISODES_SCHEMA
    from server.providers.anthropic_client import MIN_MAX_TOKENS, request_kwargs

    kw = request_kwargs(model="claude-opus-5-5", messages=[{"role": "user", "content": "x"}],
                        schema=_EPISODES_SCHEMA, max_tokens=2048)
    # Opus/Sonnet 5.5 return a 400 on forced tool use and on temperature.
    for rejected in ("tool_choice", "tools", "temperature", "top_p", "thinking"):
        assert rejected not in kw
    assert kw["max_tokens"] == MIN_MAX_TOKENS
    fmt = kw["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert "effort" not in kw["output_config"]
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]


def test_no_fallbacks_on_models_without_them():
    from server.providers.anthropic_client import request_kwargs

    kw = request_kwargs(model="claude-haiku-5-5", messages=[], effort="low")
    assert "fallbacks" not in kw and "betas" not in kw
    assert kw["output_config"] == {"effort": "low"}


def test_type_unions_become_any_of():
    from server.providers.anthropic_client import _output_schema
    from server.policies.extract import _EXTRACT_SCHEMA

    schema = _output_schema(_EXTRACT_SCHEMA)
    status = schema["properties"]["episodes"]["items"]["properties"]["status"]
    assert {"type": "null"} in status["anyOf"]
    assert "type" not in status


def _message(stop_reason="end_turn", text='{"ok": true}', category=None):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category=category) if category else None,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def test_response_text_handles_stop_reasons():
    from server.providers.anthropic_client import AnthropicRefusalError, response_text

    assert response_text(_message()) == '{"ok": true}'
    with pytest.raises(AnthropicRefusalError, match="cyber"):
        response_text(_message("refusal", category="cyber"))
    with pytest.raises(ValueError, match="max_tokens"):
        response_text(_message("max_tokens"))


def test_structured_client_parses_and_maps_refusals():
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.errors import RefusalError
    from graphiti_core.prompts.extract_nodes import ExtractedEntities
    from graphiti_core.prompts.models import Message

    from server.providers.anthropic_client import StructuredAnthropicClient

    client = StructuredAnthropicClient(LLMConfig(api_key="sk-ant-test", model="claude-opus-5-5"))
    sent = {}

    async def create(**kwargs):
        sent.update(kwargs)
        return reply

    client.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    messages = [Message(role="system", content="sys"), Message(role="user", content="text")]

    reply = _message(text='{"extracted_entities": []}')
    out = asyncio.run(client._generate_response(messages, ExtractedEntities))
    assert out == ({"extracted_entities": []}, 10, 5)
    assert sent["system"] == "sys"
    assert sent["messages"] == [{"role": "user", "content": "text"}]
    assert "properties" in sent["output_config"]["format"]["schema"]

    reply = _message("refusal", category="bio")
    with pytest.raises(RefusalError):
        asyncio.run(client._generate_response(messages, ExtractedEntities))


# --- OpenAI strict schema --------------------------------------------------------


def test_strict_schema_requires_everything_and_keeps_optional_nullable():
    from server.policies.reasoning_episode import _EPISODES_SCHEMA
    from server.providers.openai_extraction import strict_schema

    before = repr(_EPISODES_SCHEMA)
    schema = strict_schema(_EPISODES_SCHEMA)
    assert repr(_EPISODES_SCHEMA) == before, "must not mutate the shared schema"
    item = schema["properties"]["episodes"]["items"]
    assert set(item["required"]) == set(item["properties"])
    assert item["additionalProperties"] is False
    assert "null" in item["properties"]["substance_in_assistant_turns"]["type"]
    assert item["properties"]["thread_key"]["type"] == "string"  # was required: stays non-null


def test_openai_request_shape():
    from server.providers.openai_extraction import request_kwargs

    kw = request_kwargs(model="gpt-5.5", prompt="p", schema={"type": "object", "properties": {}}, schema_name="n")
    assert kw["text"]["format"]["strict"] is True
    assert kw["reasoning"] == {"effort": "none"}  # Graphiti's own pick for gpt-5.5
    assert "temperature" not in kw


# --- capture workers ---------------------------------------------------------------


@pytest.mark.parametrize("provider, fn_name", [("anthropic", "_anthropic_generate"), ("openai", "_openai_generate")])
def test_worker_generate_fn_per_provider(clean_env, provider, fn_name):
    from server.policies import reasoning_episode as re_mod
    from server.policies.extract import _EXTRACT_SCHEMA

    clean_env.setenv("CMF_LLM_PROVIDER", provider)
    assert re_mod._select_generate_fn() is getattr(re_mod, fn_name)
    wide = re_mod._select_generate_fn(schema=_EXTRACT_SCHEMA, schema_name="extract_v1")
    assert wide.func is getattr(re_mod, fn_name)
    assert wide.keywords == {"schema": _EXTRACT_SCHEMA, "schema_name": "extract_v1"}


def test_worker_uses_configured_model_not_the_limiters(clean_env, monkeypatch):
    from server.policies import reasoning_episode as re_mod
    from server.providers import anthropic_client

    _hosted(clean_env)
    clean_env.setenv("CMF_ANTHROPIC_MODEL", "claude-haiku-5-5")
    seen = {}
    monkeypatch.setattr(anthropic_client, "generate_json", lambda **kw: seen.update(kw) or "{}")
    assert re_mod._anthropic_generate("gemini-3.5-flash-lite", "prompt") == "{}"
    assert seen["model"] == "claude-haiku-5-5"
    assert seen["api_key"] == "sk-ant-test"


@pytest.mark.parametrize("provider, model", [("anthropic", "claude-sonnet-5-5"), ("openai", "gpt-5.5")])
def test_rate_limiter_is_unmetered_for_hosted(clean_env, provider, model):
    _hosted(clean_env, llm=provider)
    limiter = rate_limiter.get_default_rate_limiter()
    assert limiter.unmetered
    assert limiter.reserve() == model


def test_promotion_spacing_only_for_gemini(clean_env):
    from server.consolidation.promotion import GEMINI_INTER_CALL_DELAY, LOCAL_INTER_CALL_DELAY, default_inter_call_delay

    clean_env.setenv("GEMINI_API_KEY", "k")
    assert default_inter_call_delay() == GEMINI_INTER_CALL_DELAY
    _hosted(clean_env)
    assert default_inter_call_delay() == LOCAL_INTER_CALL_DELAY



# --- billing errors are not transient ------------------------------------------


import pytest as _pytest


@_pytest.mark.parametrize("message", [
    # The two real messages from the 2026-10-08 acceptance run.
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': "
    "'Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.'}}",
    "Error code: 429 - {'error': {'message': 'You have no credits remaining.', 'type': 'insufficient_quota', "
    "'code': 'credit_balance_exhausted'}}",
])
def test_billing_errors_fail_fast(message):
    from server.core.rate_limiter import classify_transient_error, is_billing_error

    exc = RuntimeError(message)
    assert is_billing_error(exc)
    assert classify_transient_error(exc) is None


@_pytest.mark.parametrize("message, label", [
    ("Error code: 429 - rate limit exceeded, retry after 20s", "quota"),
    ("429 RESOURCE_EXHAUSTED", "quota"),
    ("Error code: 529 - overloaded_error", "unavailable"),
])
def test_rate_limits_stay_transient(message, label):
    from server.core.rate_limiter import classify_transient_error

    assert classify_transient_error(RuntimeError(message)) == label
