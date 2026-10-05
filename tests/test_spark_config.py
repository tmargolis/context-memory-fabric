"""CMFConfig coverage for the Spark-local provider switches (Phase 1).

CMFConfig had no tests at all before this file. The cases here are the ones
that would silently mis-route traffic or corrupt a graph rather than fail
loudly: provider selection, the credential rules behind memory_enabled, and
EMBEDDING_DIM parsing.
"""

import pytest

from server.core.config import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_LOCAL_BASE_URL,
    GEMINI_PROVIDER,
    LOCAL_PROVIDER,
    load_config,
)

SPARK_VARS = (
    "CMF_LLM_PROVIDER",
    "CMF_EMBED_PROVIDER",
    "CMF_LOCAL_BASE_URL",
    "CMF_LOCAL_API_KEY",
    "CMF_LOCAL_LLM_MODEL",
    "CMF_LOCAL_EMBED_MODEL",
    "CMF_LOCAL_STRUCTURED_MODE",
    "EMBEDDING_DIM",
)


@pytest.fixture
def clean_env(monkeypatch):
    """A predictable environment, fully isolated from the developer's .env.

    Stubbing load_dotenv is required, not tidiness. load_config() calls it,
    and python-dotenv only declines to override variables that are *present*
    — a variable this fixture deletes gets repopulated straight back out of
    the real .env. Without the stub, every "this credential is missing" case
    silently tests the opposite of what it says.
    """
    monkeypatch.setattr("server.core.config.load_dotenv", lambda *a, **k: None)
    for var in (*SPARK_VARS, "GEMINI_API_KEY", "FALKORDB_DATABASE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FALKORDB_DATABASE", "test-graph")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    return monkeypatch


def test_fixture_actually_isolates_from_dotenv(clean_env):
    """Guard for the fixture itself.

    If load_dotenv stops being stubbed, the deletions below become no-ops and
    the credential tests quietly invert. Assert the isolation directly.
    """
    clean_env.delenv("GEMINI_API_KEY", raising=False)
    assert load_config().gemini_api_key is None


def test_defaults_to_gemini(clean_env):
    """Importing the module must never silently re-point a running server."""
    config = load_config()
    assert config.llm_provider == GEMINI_PROVIDER
    assert config.embed_provider == GEMINI_PROVIDER
    assert not config.llm_is_local
    assert not config.embed_is_local


def test_providers_are_independent(clean_env):
    """The hybrid must be expressible: local embeddings, Gemini extraction."""
    clean_env.setenv("CMF_EMBED_PROVIDER", "local")
    config = load_config()
    assert config.llm_provider == GEMINI_PROVIDER
    assert config.embed_provider == LOCAL_PROVIDER
    assert config.embed_is_local
    assert not config.llm_is_local


def test_provider_value_is_normalised(clean_env):
    clean_env.setenv("CMF_LLM_PROVIDER", "  LOCAL  ")
    assert load_config().llm_provider == LOCAL_PROVIDER


def test_unknown_provider_raises(clean_env):
    """A typo must not silently degrade to the metered provider."""
    clean_env.setenv("CMF_LLM_PROVIDER", "lcoal")
    with pytest.raises(ValueError, match="not a recognised provider"):
        load_config()


def test_local_defaults_point_at_the_tunnel(clean_env):
    config = load_config()
    assert config.local_base_url == DEFAULT_LOCAL_BASE_URL
    assert "127.0.0.1" in config.local_base_url, "must never default to a public address"
    assert config.local_structured_mode == "json_schema", (
        "Mode A is the verified default: Mode B makes GLM echo graphiti's schema back"
    )


def test_memory_enabled_without_gemini_key_when_fully_local(clean_env):
    """The regression this phase exists to prevent.

    A fully local deployment has no GEMINI_API_KEY. The previous
    unconditional `bool(self.gemini_api_key)` reported memory as disabled on
    an otherwise correctly configured box, which would have unregistered the
    memory tools at startup.
    """
    clean_env.delenv("GEMINI_API_KEY", raising=False)
    clean_env.setenv("CMF_LLM_PROVIDER", "local")
    clean_env.setenv("CMF_EMBED_PROVIDER", "local")
    assert load_config().memory_enabled is True


def test_memory_disabled_when_a_gemini_provider_lacks_its_key(clean_env):
    """The hybrid needs the credentials of both halves, not either half."""
    clean_env.delenv("GEMINI_API_KEY", raising=False)
    clean_env.setenv("CMF_LLM_PROVIDER", "gemini")
    clean_env.setenv("CMF_EMBED_PROVIDER", "local")
    assert load_config().memory_enabled is False


def test_memory_disabled_when_local_has_no_base_url(clean_env):
    clean_env.setenv("CMF_LLM_PROVIDER", "local")
    clean_env.setenv("CMF_EMBED_PROVIDER", "local")
    clean_env.setenv("CMF_LOCAL_BASE_URL", "   ")
    assert load_config().memory_enabled is False


def test_memory_disabled_without_a_graph_name(clean_env):
    """Unchanged from before Phase 1: CMF never guesses a graph name."""
    clean_env.setenv("FALKORDB_DATABASE", "  ")
    assert load_config().memory_enabled is False


def test_embedding_dim_defaults_to_the_gemini_width(clean_env):
    assert load_config().embedding_dim == DEFAULT_EMBEDDING_DIM == 1024


def test_embedding_dim_reads_the_nomic_width(clean_env):
    clean_env.setenv("EMBEDDING_DIM", "768")
    assert load_config().embedding_dim == 768


@pytest.mark.parametrize("bad", ["768.5", "wide", "0", "-1"])
def test_embedding_dim_rejects_nonsense(clean_env, bad):
    """A bad width corrupts vectors silently, so it must fail at load time."""
    clean_env.setenv("EMBEDDING_DIM", bad)
    with pytest.raises(ValueError, match="EMBEDDING_DIM"):
        load_config()


def test_early_dotenv_load_precedes_graphiti_import():
    """server/__init__.py must populate the environment before graphiti_core
    freezes EMBEDDING_DIM into a module constant.

    Without it, setting EMBEDDING_DIM in .env has no effect whatsoever and
    graphiti_core.search falls back to a zero vector of the wrong width.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import server; import graphiti_core.embedder.client as ec; print(ec.EMBEDDING_DIM)",
        ],
        capture_output=True,
        text=True,
        env={"EMBEDDING_DIM": "768", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "768"
