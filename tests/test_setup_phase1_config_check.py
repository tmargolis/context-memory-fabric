"""Setup simplification Phase 1, item 1.1: the startup config check
(docs/setup-simplification-plan.md), plus the /health fields `cmf status`
reads for its provider-test and embedding-width rows (item 3.3).

FalkorDB and the providers are faked; no live calls.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import redis

from server.core import startup_check
from server.core import status as server_status
from server.core.config import load_config


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CMF_STARTUP_CHECK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FALKORDB_HOST", "localhost")
    monkeypatch.setenv("FALKORDB_PORT", "6380")
    monkeypatch.setenv("FALKORDB_DATABASE", "CMF-local")
    monkeypatch.setenv("CMF_LLM_PROVIDER", "local")
    monkeypatch.setenv("CMF_EMBED_PROVIDER", "local")
    monkeypatch.setenv("CMF_CAPTURE_LLM_PROVIDER", "local")
    monkeypatch.setenv("CMF_LOCAL_BASE_URL", "http://127.0.0.1:12345/v1")
    monkeypatch.setenv("EMBEDDING_DIM", "768")
    startup_check.LAST_RESULT = None


def _falkordb(graphs=("CMF-local",), width=768, ping_error=None):
    """Patch both FalkorDB clients: GRAPH.LIST answers `graphs`, and one stored
    vector of `width` (None: the graph holds no vectors yet)."""
    rc = MagicMock()
    if ping_error:
        rc.ping.side_effect = ping_error
    rc.execute_command.return_value = [g.encode() for g in graphs]
    graph = MagicMock()
    graph.ro_query.return_value = SimpleNamespace(result_set=[[[0.1] * width]] if width else [])
    fc = MagicMock()
    fc.select_graph.return_value = graph
    return (
        patch("server.core.falkordb_conn.redis_client", return_value=rc),
        patch("server.core.falkordb_conn.falkordb_client", return_value=fc),
    )


def _check(**falkordb):
    a, b = _falkordb(**falkordb)
    with a, b:
        return startup_check.check(falkordb_wait=0, sleep=lambda s: None)


def test_good_local_setup_passes():
    result = _check()
    assert result.problems == [] and result.warnings == []
    assert result.graph_embedding_dim == 768


def test_anthropic_embeddings_is_one_sentence():
    import os

    os.environ["CMF_EMBED_PROVIDER"] = "anthropic"
    result = _check()
    assert len(result.problems) == 1 and "anthropic has no embeddings API" in result.problems[0]


def test_missing_and_placeholder_keys(monkeypatch):
    monkeypatch.setenv("CMF_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("CMF_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "your_openai_api_key_here")
    result = _check()
    assert result.problems == [
        "The LLM provider is anthropic, but ANTHROPIC_API_KEY isn't set; add a real anthropic API key to .env.",
        "The embeddings provider is openai, but OPENAI_API_KEY is still placeholder text; add a real openai API key to .env.",
    ]


def test_capture_key_is_only_a_warning(monkeypatch):
    monkeypatch.setenv("CMF_CAPTURE_LLM_PROVIDER", "gemini")
    result = _check()
    assert result.problems == []
    assert "background capture provider is gemini, but GEMINI_API_KEY isn't set" in result.warnings[0]


def test_falkordb_down_after_waiting():
    sleeps = []
    a, b = _falkordb(ping_error=redis.exceptions.ConnectionError("Connection refused"))
    with a, b, patch("server.core.startup_check.time.monotonic", side_effect=[0, 0, 1, 2, 31]):
        result = startup_check.check(falkordb_wait=30, sleep=sleeps.append)
    assert len(result.problems) == 1 and "FalkorDB doesn't answer at localhost:6380" in result.problems[0]
    assert sleeps == [2, 2, 2]  # tried at 0, 1 and 2 s; 31 s is past the 30 s deadline


def test_falkordb_login_rejected_does_not_wait():
    sleeps = []
    a, b = _falkordb(ping_error=redis.exceptions.AuthenticationError("WRONGPASS invalid username-password pair"))
    with a, b:
        result = startup_check.check(falkordb_wait=30, sleep=sleeps.append)
    assert "FALKORDB_PASSWORD" in result.problems[0] and sleeps == []


def test_embedding_width_mismatch(monkeypatch):
    monkeypatch.setenv("EMBEDDING_DIM", "1024")
    result = _check(width=768)
    assert result.graph_embedding_dim == 768
    assert result.problems[0].startswith("EMBEDDING_DIM is 1024, but graph 'CMF-local' holds 768-wide embeddings")


def test_new_or_empty_graph_has_no_width_to_match():
    assert _check(graphs=("other",)).problems == []
    empty = _check(width=None)
    assert empty.problems == [] and empty.graph_embedding_dim is None


def test_run_startup_check_exits_unless_warn(monkeypatch, caplog):
    monkeypatch.setenv("CMF_LLM_PROVIDER", "openai")
    a, b = _falkordb()
    with a, b:
        assert startup_check.run_startup_check(falkordb_wait=0) is False
        assert "Config check: The LLM provider is openai" in caplog.text
        assert startup_check.LAST_RESULT.problems
        monkeypatch.setenv("CMF_STARTUP_CHECK", "warn")
        assert startup_check.run_startup_check(falkordb_wait=0) is True


# --- live provider tests --------------------------------------------------------------------------


def _response(status, body):
    return SimpleNamespace(status_code=status, json=lambda: body)


def test_provider_test_local_lists_model():
    config = load_config()
    calls = []

    def get(url, headers):
        calls.append(url)
        return _response(200, {"data": [{"id": config.local_llm_model}, {"id": config.local_embed_model}]})

    tests = startup_check.provider_tests(get)
    assert [t["role"] for t in tests] == ["LLM", "embeddings", "background capture"]
    assert all(t["ok"] for t in tests), tests
    assert calls == ["http://127.0.0.1:12345/v1/models"] * 2  # capture reuses the LLM's answer


def test_provider_test_reports_rejected_key_and_missing_model(monkeypatch):
    monkeypatch.setenv("CMF_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    config = load_config()
    rejected = startup_check.provider_test("LLM", "anthropic", config, lambda u, h: _response(401, {}))
    assert rejected == {"role": "LLM", "provider": "anthropic", "ok": False,
                        "detail": "anthropic rejected the API key (ANTHROPIC_API_KEY)"}
    missing = startup_check.provider_test("LLM", "anthropic", config, lambda u, h: _response(200, {"data": [{"id": "claude-other"}]}))
    assert not missing["ok"] and "doesn't list the model claude-sonnet-5-5" in missing["detail"]


def test_provider_test_unreachable():
    def get(url, headers):
        raise ConnectionError("refused")

    out = startup_check.provider_test("LLM", "local", load_config(), get)
    assert not out["ok"] and out["detail"] == "no answer from http://127.0.0.1:12345/v1/models (ConnectionError)"


def test_gemini_model_names_are_unprefixed(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    out = startup_check.provider_test("embeddings", "gemini", load_config(),
                                      lambda u, h: _response(200, {"models": [{"name": "models/gemini-embedding-001"}]}))
    assert out["ok"]


# --- /health fields -------------------------------------------------------------------------------


def test_health_reports_config_check_and_embedding_width(tmp_path, monkeypatch):
    monkeypatch.setenv("CMF_STATE_DIR", str(tmp_path))
    a, b = _falkordb(width=768)
    with a, b:
        startup_check.run_startup_check(falkordb_wait=0)
        snap = server_status.snapshot()
    assert snap["config_check"]["problems"] == []
    assert snap["embedding"] == {"configured": 768, "graph": 768}
    assert "provider_tests" not in snap


def test_health_provider_tests_on_request(tmp_path, monkeypatch):
    monkeypatch.setenv("CMF_STATE_DIR", str(tmp_path))
    a, b = _falkordb()
    with a, b, patch.object(startup_check, "provider_tests", return_value=[{"role": "LLM", "ok": True}]):
        snap = server_status.snapshot(provider_tests=True)
    assert snap["provider_tests"] == [{"role": "LLM", "ok": True}]


# --- 1.6: port guard ------------------------------------------------------------------------------


def test_port_guard_free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert startup_check.port_problem("127.0.0.1", port) is None


@pytest.mark.parametrize("listen_on", ["127.0.0.1", "0.0.0.0"])
def test_port_guard_taken(listen_on):
    import socket

    with socket.socket() as s:
        s.bind((listen_on, 0))
        s.listen()
        port = s.getsockname()[1]
        problem = startup_check.port_problem("127.0.0.1", port)
    assert problem == (
        f"Another process is already on port {port}, so this CMF server can't start. "
        "Run `cmf status` (or open CMF Setup) to see which one it is."
    )
