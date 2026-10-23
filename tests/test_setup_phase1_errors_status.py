"""Setup simplification Phase 1, items 1.2-1.5 (docs/setup-simplification-plan.md).

1.2 foreseeable failures reach the model as one actionable sentence (ToolError),
1.3 an empty or not-yet-created graph is "No memories yet", not an error,
1.4 every background remember records committed / failed / lost,
1.5 /health and capture_health report one status snapshot.

No live FalkorDB or model calls: failures are constructed from the real SDK
exception classes.
"""

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import openai
import pytest
import redis
from google.genai import errors as genai_errors
from mcp.server.mcpserver.exceptions import ToolError

from server.core import status as server_status
from server.core import tool_errors
from server.core.config import load_config
from server.core.rate_limiter import GeminiQuotaExhaustedError
from server.memory_writes import COMMITTED, FAILED, LOST, QUEUED, BackgroundWriteLog, format_write_status


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("FALKORDB_HOST", "localhost")
    monkeypatch.setenv("FALKORDB_PORT", "6380")
    monkeypatch.setenv("FALKORDB_DATABASE", "CMF-local")
    monkeypatch.setenv("CMF_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("CMF_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("CMF_LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1")


def _classify(exc):
    return tool_errors.classify(exc, load_config())


def _http_error(cls, status, url="https://api.anthropic.com/v1/messages", message="boom"):
    request = httpx.Request("POST", url)
    return cls(message, response=httpx.Response(status, request=request), body=None)


# --- 1.2: classifier ---------------------------------------------------------------------------


def test_falkordb_unreachable_names_address_and_graph():
    result = _classify(redis.exceptions.ConnectionError("Error 61 connecting to localhost:6380. Connection refused."))
    assert result.kind == tool_errors.FALKORDB_UNREACHABLE
    assert "localhost:6380" in result.message and "CMF-local" in result.message


def test_falkordb_login_rejected():
    result = _classify(redis.exceptions.AuthenticationError("WRONGPASS invalid username-password pair"))
    assert result.kind == tool_errors.FALKORDB_AUTH
    assert "FALKORDB_PASSWORD" in result.message


@pytest.mark.parametrize(
    "message",
    ["Invalid graph operation on empty key", "RediSearch: Unknown index name", "no such index"],
)
def test_missing_graph_or_index_is_empty_graph(message):
    assert _classify(redis.exceptions.ResponseError(message)).kind == tool_errors.EMPTY_GRAPH


def test_embedding_width_probe_failure():
    exc = RuntimeError(
        "Embedder returned 768-dimension vectors but EMBEDDING_DIM is 1024. Writing these into a "
        "1024-dimension graph would corrupt it silently."
    )
    result = _classify(exc)
    assert result.kind == tool_errors.EMBEDDING_MISMATCH
    assert "FALKORDB_DATABASE" in result.message


def test_missing_key():
    exc = RuntimeError("ANTHROPIC_API_KEY is not set, but anthropic is a configured provider. Add it to .env.")
    result = _classify(exc)
    assert result.kind == tool_errors.MISSING_KEY
    assert "ANTHROPIC_API_KEY" in result.message


def test_anthropic_rejected_key_names_env_var():
    result = _classify(_http_error(anthropic.AuthenticationError, 401, message="invalid x-api-key"))
    assert result.kind == tool_errors.PROVIDER_AUTH
    assert "ANTHROPIC_API_KEY" in result.message


def test_gemini_invalid_key():
    exc = genai_errors.ClientError(400, {"error": {"message": "API key not valid. Please pass a valid API key."}})
    result = _classify(exc)
    assert result.kind == tool_errors.PROVIDER_AUTH
    assert "GEMINI_API_KEY" in result.message


def test_unfunded_account_is_billing_not_quota():
    exc = _http_error(
        openai.RateLimitError, 429, url="https://api.openai.com/v1/embeddings",
        message="You exceeded your current quota. insufficient_quota",
    )
    assert _classify(exc).kind == tool_errors.PROVIDER_BILLING


def test_rate_limit_and_local_quota():
    exc = _http_error(anthropic.RateLimitError, 429, message="rate limit exceeded")
    assert _classify(exc).kind == tool_errors.PROVIDER_QUOTA
    assert _classify(GeminiQuotaExhaustedError("all models exhausted")).kind == tool_errors.PROVIDER_QUOTA


def test_model_not_found():
    exc = _http_error(anthropic.NotFoundError, 404, message="not_found_error: model: claude-nonexistent")
    assert _classify(exc).kind == tool_errors.MODEL_NOT_FOUND


def test_local_model_server_unreachable_names_url_and_docker_host():
    exc = openai.APIConnectionError(request=httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions"))
    result = _classify(exc)
    assert result.kind == tool_errors.PROVIDER_UNREACHABLE
    assert "127.0.0.1:11434" in result.message and "host.docker.internal" in result.message


def test_wrapped_cause_is_found():
    try:
        try:
            raise redis.exceptions.ConnectionError("Connection refused")
        except redis.exceptions.ConnectionError as inner:
            raise RuntimeError("search failed") from inner
    except RuntimeError as outer:
        assert _classify(outer).kind == tool_errors.FALKORDB_UNREACHABLE


def test_unknown_errors_are_not_classified():
    assert _classify(ValueError("a real bug")) is None
    assert _classify(KeyError("fact")) is None


# --- 1.2 / 1.3: tools ----------------------------------------------------------------------------


def test_recall_mem_raises_tool_error_with_sentence():
    import server.mcp as mcp

    with patch.object(mcp, "recall_memory", AsyncMock(side_effect=redis.exceptions.ConnectionError("refused"))):
        with pytest.raises(ToolError) as info:
            asyncio.run(mcp.recall_mem(query="anything"))
    assert "can't reach its database" in str(info.value)


def test_recall_mem_on_empty_graph_says_no_memories_yet():
    import server.mcp as mcp

    err = redis.exceptions.ResponseError("Invalid graph operation on empty key")
    with patch.object(mcp, "recall_memory", AsyncMock(side_effect=err)):
        assert asyncio.run(mcp.recall_mem(query="anything")) == tool_errors.NO_MEMORIES_YET


def test_recall_mem_reraises_unknown_errors():
    import server.mcp as mcp

    with patch.object(mcp, "recall_memory", AsyncMock(side_effect=ValueError("bug"))):
        with pytest.raises(ValueError):
            asyncio.run(mcp.recall_mem(query="anything"))


def test_remember_failure_before_queueing_says_nothing_saved():
    import server.mcp as mcp

    err = RuntimeError("OPENAI_API_KEY is not set, but openai is a configured provider. Add it to .env.")
    with patch.object(mcp, "remember_queued", AsyncMock(side_effect=err)):
        with pytest.raises(ToolError) as info:
            asyncio.run(mcp.remember(content="x"))
    assert str(info.value).startswith("Nothing was saved.")
    assert "OPENAI_API_KEY" in str(info.value)


class _Knowledge:
    def is_configured(self):
        return False

    def search(self, *a, **k):
        return []


class _FailingMemory:
    def __init__(self, exc):
        self.exc = exc

    async def recall(self, *a, **k):
        raise self.exc


def test_get_context_keeps_answering_when_memory_is_down():
    from server.context import get_context

    out = asyncio.run(
        get_context(
            "atlas",
            knowledge_provider=_Knowledge(),
            memory_provider=_FailingMemory(redis.exceptions.ConnectionError("refused")),
            knowledge_sources=[],
        )
    )
    assert "DURABLE KNOWLEDGE" in out
    assert "Episodic memory is unavailable: CMF can't reach its database" in out


def test_get_context_empty_graph_says_no_memories_yet():
    from server.context import get_context

    out = asyncio.run(
        get_context(
            "atlas",
            knowledge_provider=_Knowledge(),
            memory_provider=_FailingMemory(redis.exceptions.ResponseError("Invalid graph operation on empty key")),
            knowledge_sources=[],
        )
    )
    assert tool_errors.NO_MEMORIES_YET in out


def test_get_context_reraises_unknown_memory_errors():
    from server.context import get_context

    with pytest.raises(ValueError):
        asyncio.run(
            get_context(
                "atlas", knowledge_provider=_Knowledge(), memory_provider=_FailingMemory(ValueError("bug")),
                knowledge_sources=[],
            )
        )


# --- 1.4: background write log -------------------------------------------------------------------


def test_write_log_lifecycle(tmp_path):
    log = BackgroundWriteLog(tmp_path / "j.db")
    log.queued("ep-1", "CMF-local")
    assert log.get("ep-1")["status"] == QUEUED
    log.committed("ep-1")
    assert log.get("ep-1")["status"] == COMMITTED
    log.queued("ep-2", "CMF-local")
    log.failed("ep-2", tool_errors.PROVIDER_AUTH, "anthropic rejected CMF's API key.")
    row = log.get("ep-2")
    assert (row["status"], row["error_kind"]) == (FAILED, tool_errors.PROVIDER_AUTH)
    assert [r["episode_name"] for r in log.recent(5)] == ["ep-2", "ep-1"]
    log.close()


def test_queued_write_from_before_restart_reads_as_lost(tmp_path):
    first = BackgroundWriteLog(tmp_path / "j.db")
    first.queued("ep-1", "CMF-local")
    first.close()
    later = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    second = BackgroundWriteLog(tmp_path / "j.db", process_started_at=later)
    row = second.get("ep-1")
    assert row["status"] == LOST and "restarted" in row["error_message"]
    second.close()


def test_format_write_status():
    assert "No background write named `nope`" in format_write_status([], "nope")
    assert format_write_status([]) == "No background memory writes recorded yet."


def _identity_patch():
    graphiti = MagicMock()
    return (
        patch("server.providers.memory_graphiti.get_graphiti", return_value=graphiti),
        patch(
            "server.providers.memory_graphiti.resolve_remember_identity",
            AsyncMock(return_value=("claude_code-atlas-001", "atlas", "claude_code")),
        ),
    )


async def _queue_and_wait(remember_mock):
    from server.providers import memory_graphiti

    with patch("server.providers.memory_graphiti.remember", remember_mock):
        res = await memory_graphiti.remember_queued(content="x", project="atlas", harness="claude_code")
        await asyncio.gather(*list(memory_graphiti._BACKGROUND_REMEMBER_TASKS))
    return res


def test_remember_queued_records_commit_and_points_at_memory_status():
    from server.memory_writes import get_default_write_log

    g, ident = _identity_patch()
    with g, ident:
        res = asyncio.run(_queue_and_wait(AsyncMock(return_value={"status": "success"})))
    assert "memory_status(episode='claude_code-atlas-001')" in res["message"]
    assert get_default_write_log().get("claude_code-atlas-001")["status"] == COMMITTED


def test_remember_queued_records_classified_failure():
    from server.memory_writes import get_default_write_log

    g, ident = _identity_patch()
    err = _http_error(anthropic.AuthenticationError, 401, message="invalid x-api-key")
    with g, ident:
        asyncio.run(_queue_and_wait(AsyncMock(side_effect=err)))
    row = get_default_write_log().get("claude_code-atlas-001")
    assert row["status"] == FAILED and row["error_kind"] == tool_errors.PROVIDER_AUTH
    assert "ANTHROPIC_API_KEY" in row["error_message"]


def test_memory_status_tool():
    import server.mcp as mcp
    from server.memory_writes import get_default_write_log

    get_default_write_log().queued("ep-x", "CMF-local")
    out = asyncio.run(mcp.memory_status(episode="ep-x"))
    assert "`ep-x`" in out and "**queued**" in out and "check again in a minute" in out


# --- 1.5: status snapshot and /health ------------------------------------------------------------


def _journal_with_calls(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE events (event_type TEXT, harness TEXT, observed_at TEXT, metadata_json TEXT)"
    )
    conn.executemany("INSERT INTO events VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_clients_seen_last_24h(tmp_path):
    now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
    db = tmp_path / "journal.db"
    _journal_with_calls(db, [
        ("mcp_tool_call", "claude_desktop", (now - timedelta(hours=1)).isoformat(), '{"client_version": "1.0.0"}'),
        ("mcp_tool_call", "claude_desktop", (now - timedelta(hours=2)).isoformat(), '{"client_version": "1.0.0"}'),
        ("mcp_tool_call", "codex", (now - timedelta(days=3)).isoformat(), "{}"),
        ("transcript_turn", "claude_code", (now - timedelta(hours=1)).isoformat(), "{}"),
    ])
    seen = server_status.clients_seen(db, now=now)
    assert [(c["harness"], c["calls"], c["client_version"]) for c in seen] == [("claude_desktop", 2, "1.0.0")]


def test_snapshot_reports_unreachable_falkordb_without_raising(tmp_path):
    with patch("server.core.falkordb_conn.redis_client", side_effect=redis.exceptions.ConnectionError("refused")), \
         patch.object(server_status, "clients_seen", return_value=[]):
        snap = server_status.snapshot()
    assert snap["graph"] == "CMF-local"
    assert snap["falkordb"]["reachable"] is False
    assert "localhost:6380" in snap["falkordb"]["address"]
    assert snap["providers"]["llm"] == "anthropic" and snap["providers"]["embeddings"] == "openai"
    lines = "\n".join(server_status.format_status_lines(snap))
    assert "NOT reachable at localhost:6380" in lines


def _request(headers):
    from starlette.requests import Request

    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/health", "headers": raw, "query_string": b""})


def test_health_full_snapshot_for_direct_local_request():
    import server.mcp as mcp

    with patch.object(server_status, "snapshot", return_value={"status": "ok", "graph": "CMF-local"}):
        response = asyncio.run(mcp.health(_request({"host": "127.0.0.1:8000"})))
    assert b"CMF-local" in response.body


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Tailscale-Funnel-Request", "Forwarded"])
def test_health_minimal_through_proxy_or_tunnel(header):
    import server.mcp as mcp

    with patch.object(server_status, "snapshot", side_effect=AssertionError("must not build full snapshot")):
        response = asyncio.run(mcp.health(_request({"host": "cmf.example.ts.net", header: "?1"})))
    assert b"CMF-local" not in response.body and b'"status":"ok"' in response.body
