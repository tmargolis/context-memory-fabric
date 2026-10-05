"""Phase 5: reasoning-episode extraction on the local provider.

No network: the local generate function is exercised against a stubbed
LMStudioCompatClient, and provider selection is checked by identity.
"""

import sys
import types

import pytest

from server.policies.reasoning_episode import (
    REASONING_POLICY_VERSION,
    ReasoningEpisodePolicyV1,
    _default_generate,
    _EPISODES_SCHEMA,
    _is_retryable_capacity_error,
    _local_generate,
    _select_generate_fn,
)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr("server.core.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("FALKORDB_DATABASE", "test-graph")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("CMF_LOCAL_LLM_MODEL", "zai-org/glm-4.7-flash")
    return monkeypatch


# --- transient classification --------------------------------------------


@pytest.mark.parametrize(
    "message",
    ["503 UNAVAILABLE", '{"error": "Model unloaded."}', "please try again later", "overloaded"],
)
def test_capacity_errors_are_retryable(message):
    assert _is_retryable_capacity_error(Exception(message)) is True


@pytest.mark.parametrize("message", ["429 RESOURCE_EXHAUSTED", "quota exceeded", "bad schema"])
def test_quota_and_real_errors_are_not_retried_here(message):
    """Quota is re-raised as GeminiQuotaExhaustedError by evaluate_window so
    the pipeline stops clean, rather than burning retries on an immovable wall."""
    assert _is_retryable_capacity_error(Exception(message)) is False


def test_model_unloaded_is_covered_by_the_shared_classifier():
    """The regression this consolidation prevents: LM Studio's Auto-Evict
    string was absent from the policy's old private marker tuple."""
    assert _is_retryable_capacity_error(Exception("Model unloaded.")) is True


# --- provider selection ---------------------------------------------------


def test_selects_gemini_by_default(env):
    env.setenv("CMF_LLM_PROVIDER", "gemini")
    assert _select_generate_fn() is _default_generate


def test_selects_local_when_configured(env):
    env.setenv("CMF_LLM_PROVIDER", "local")
    assert _select_generate_fn() is _local_generate


def test_policy_picks_up_the_provider_at_construction(env):
    env.setenv("CMF_LLM_PROVIDER", "local")
    from server.core.rate_limiter import reset_default_rate_limiter

    reset_default_rate_limiter()
    policy = ReasoningEpisodePolicyV1()
    assert policy._generate is _local_generate
    assert policy._rate_limiter.unmetered is True
    reset_default_rate_limiter()


def test_injected_generate_fn_still_wins(env):
    """Tests and the offline probe rely on this override."""
    env.setenv("CMF_LLM_PROVIDER", "local")
    sentinel = lambda model, prompt: "{}"  # noqa: E731
    assert ReasoningEpisodePolicyV1(generate_fn=sentinel)._generate is sentinel


# --- version -------------------------------------------------------------


def test_version_bumped_for_the_model_change():
    """A different extraction model is a different policy: Gemini-derived and
    GLM-derived episodes must not share a version bucket, or the Phase 7
    quality comparison has nothing to compare."""
    assert REASONING_POLICY_VERSION == "0.5"
    assert ReasoningEpisodePolicyV1.version == REASONING_POLICY_VERSION


def test_downstream_defaults_track_the_constant():
    """A literal "0.2" repeated across four modules is how a version bump
    silently stops matching rows. All of these must follow the bump."""
    import inspect

    from server.consolidation.promotion import tier1_review_queue
    from server.consolidation.store import ConsolidationStore
    from server.review.queue import review_queue

    assert inspect.signature(review_queue).parameters["policy_version"].default == REASONING_POLICY_VERSION
    assert inspect.signature(tier1_review_queue).parameters["policy_version"].default == REASONING_POLICY_VERSION
    assert (
        inspect.signature(ConsolidationStore.mark_superseded_by_reasoning)
        .parameters["reasoning_version"].default == REASONING_POLICY_VERSION
    )


# --- _local_generate ------------------------------------------------------


class _StubClient:
    """Captures the request and returns a canned completion."""

    def __init__(self, content="{}", raises=None):
        self.calls: list[dict] = []
        self._content = content
        self._raises = raises
        outer = self

        class _Completions:
            async def create(self, **kwargs):
                outer.calls.append(kwargs)
                if outer._raises:
                    exc, outer._raises = outer._raises, None
                    raise exc
                msg = types.SimpleNamespace(content=outer._content)
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

        self.chat = types.SimpleNamespace(completions=_Completions())
        self.closed = 0

    async def close(self):
        self.closed += 1


def _patch_client(monkeypatch, stub):
    module = types.ModuleType("server.providers.lmstudio_client")
    module.LMStudioCompatClient = lambda base_url, api_key: stub
    monkeypatch.setitem(sys.modules, "server.providers.lmstudio_client", module)


def test_local_generate_returns_content(env, monkeypatch):
    env.setenv("CMF_LLM_PROVIDER", "local")
    stub = _StubClient(content='{"episodes": []}')
    _patch_client(monkeypatch, stub)
    assert _local_generate("zai-org/glm-4.7-flash", "prompt") == '{"episodes": []}'


def test_local_generate_sends_a_schema_in_json_schema_mode(env, monkeypatch):
    env.setenv("CMF_LLM_PROVIDER", "local")
    env.setenv("CMF_LOCAL_STRUCTURED_MODE", "json_schema")
    stub = _StubClient(content="{}")
    _patch_client(monkeypatch, stub)
    _local_generate("m", "prompt")
    fmt = stub.calls[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["schema"] is _EPISODES_SCHEMA


def test_local_generate_sends_text_in_the_other_mode(env, monkeypatch):
    env.setenv("CMF_LLM_PROVIDER", "local")
    env.setenv("CMF_LOCAL_STRUCTURED_MODE", "json_object")
    stub = _StubClient(content="{}")
    _patch_client(monkeypatch, stub)
    _local_generate("m", "prompt")
    assert stub.calls[0]["response_format"] == {"type": "text"}


def test_local_generate_retries_a_capacity_error(env, monkeypatch):
    """Auto-Evict mid-run must not abort the batch."""
    env.setenv("CMF_LLM_PROVIDER", "local")
    stub = _StubClient(content='{"episodes": []}', raises=Exception('{"error": "Model unloaded."}'))
    _patch_client(monkeypatch, stub)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    assert _local_generate("m", "prompt") == '{"episodes": []}'
    assert len(stub.calls) == 2, "should have retried exactly once"


def test_local_generate_does_not_retry_a_real_error(env, monkeypatch):
    env.setenv("CMF_LLM_PROVIDER", "local")
    stub = _StubClient(raises=ValueError("malformed schema"))
    _patch_client(monkeypatch, stub)
    with pytest.raises(ValueError):
        _local_generate("m", "prompt")
    assert len(stub.calls) == 1


def test_schema_covers_every_field_to_episode_reads(env):
    """Guard against the schema and _to_episode drifting apart."""
    props = _EPISODES_SCHEMA["properties"]["episodes"]["items"]["properties"]
    for field in ("reasoning_kind", "statement", "turn_numbers", "confidence", "status"):
        assert field in props, f"{field} is read by _to_episode but absent from the schema"


def test_schema_requires_thread_key():
    """thread_key must not be nullable in the grammar.

    Gemini populated it on 1,242 of 1,243 real rows; GLM under constrained
    decoding returned null wherever the schema permitted it. The field is
    load-bearing -- consolidation/threads.py matches conversations on it and
    review/projects.py buckets by it -- so a nullable slot is a silent
    regression in thread continuity, not a harmless omission.
    """
    item = _EPISODES_SCHEMA["properties"]["episodes"]["items"]
    assert item["properties"]["thread_key"] == {"type": "string"}
    assert "thread_key" in item["required"]


def test_genuinely_optional_fields_stay_nullable():
    """Requiring everything would push the model to invent values. Only
    fields with corpus evidence of a real null-rate problem get promoted:
    thread_key (2026-09-08, Gemini 1242/1243 vs. GLM null-whenever-permitted),
    then driving_question/rationale (2026-09-19, 0/25 populated under a
    prompt-only ask -- see REASONING_POLICY_VERSION 0.5's note). status,
    alternatives, and thread_title have no such evidence and stay optional."""
    props = _EPISODES_SCHEMA["properties"]["episodes"]["items"]["properties"]
    for field in ("status", "alternatives", "thread_title"):
        assert "null" in props[field]["type"], f"{field} should remain optional"
    for field in ("driving_question", "rationale"):
        assert props[field]["type"] == "string", f"{field} should be required non-nullable (promoted 2026-09-19)"


# --- CLI wiring -----------------------------------------------------------


@pytest.mark.parametrize("command", ["stats", "queue", "export"])
def test_cli_commands_accept_policy_version(command):
    """Every command that filters by policy version must expose the flag.

    Regression guard: `stats` was defined as a chained one-liner
    (`sub.add_parser(...).set_defaults(...)`), so a bulk edit adding the flag
    to the other subparsers silently skipped it -- while cmd_stats had already
    been changed to read args.policy_version. The result was an
    AttributeError on every `stats` invocation, caught by hand rather than by
    a test, because nothing covered CLI argument wiring.
    """
    from server.review.cli import build_parser

    argv = [command] + (["--out", "/tmp/x.json"] if command == "export" else [])
    args = build_parser().parse_args(argv)
    assert args.policy_version == REASONING_POLICY_VERSION


@pytest.mark.parametrize("command", ["stats", "queue", "export"])
def test_cli_policy_version_is_overridable(command):
    """The 1,243-row backlog sits at 0.2 and must stay reachable."""
    from server.review.cli import build_parser

    argv = [command] + (["--out", "/tmp/x.json"] if command == "export" else []) + ["--policy-version", "0.2"]
    assert build_parser().parse_args(argv).policy_version == "0.2"
