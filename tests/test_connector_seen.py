"""Calls through the claude.ai connector, told apart by how they signed in (v1.0.4).

Cowork and Chat reach CMF through the claude.ai connector announcing
themselves as "claude-code", the CLI's own name, so Status counted their
calls and captures under Claude Code and showed the connector as unused.
The capture middleware now records `via` (connector / local / none) on each
tool call, and status reports connector calls on their own.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace

from server.capture.middleware import auth_via, build_tool_call_event
from server.core import status
from server.core.oauth_provider import LOCAL_CLIENT_ID


def _request(client_id):
    token = SimpleNamespace(client_id=client_id)
    return SimpleNamespace(scope={"user": SimpleNamespace(access_token=token)})


def test_auth_via_tells_connector_from_local_token():
    assert auth_via(_request("claude-ai-oauth-client")) == "connector"
    assert auth_via(_request(LOCAL_CLIENT_ID)) == "local"
    assert auth_via(SimpleNamespace(scope={})) is None
    assert auth_via(None) is None


def test_tool_call_event_carries_via():
    session = SimpleNamespace(client_params=SimpleNamespace(client_info=SimpleNamespace(name="claude-code", version="2.1")))
    event, _ = build_tool_call_event(tool_name="recall_mem", arguments={"query": "x"}, result="ok",
                                     session=session, request_id=7, via="connector")
    assert event.source.harness == "claude_code"
    assert event.metadata["via"] == "connector"


def _journal(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE events (event_type TEXT, harness TEXT, observed_at TEXT, metadata_json TEXT, content_json TEXT)")
    conn.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_connector_calls_are_reported_apart_from_the_apps(tmp_path):
    now = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)
    db = tmp_path / "journal.db"
    via = json.dumps({"via": "connector"})
    _journal(db, [
        ("mcp_tool_call", "claude_code", (now - timedelta(hours=1)).isoformat(), via, json.dumps({"tool": "recall_mem"})),
        ("mcp_tool_call", "claude_code", (now - timedelta(hours=2)).isoformat(), via, json.dumps({"tool": "capture_session"})),
        ("mcp_tool_call", "claude_code", (now - timedelta(days=3)).isoformat(), via, json.dumps({"tool": "capture_session"})),
        ("mcp_tool_call", "claude_code", (now - timedelta(hours=3)).isoformat(), json.dumps({"via": "local"}), json.dumps({"tool": "get_context"})),
        ("mcp_tool_call", "codex", (now - timedelta(hours=1)).isoformat(), "{}", json.dumps({"tool": "recall_mem"})),
    ])

    seen = {c["harness"]: c["calls"] for c in status.clients_seen(db, now=now)}
    assert seen == {"claude_code": 1, "codex": 1}  # the local-token call only

    conn = status.connector_seen(db, now=now)
    assert conn["calls"] == 2 and conn["names"] == ["claude_code"]
    assert conn["captures"] == 2  # capture_session over 7 days
    assert conn["last_seen"] == (now - timedelta(hours=1)).isoformat()


def test_no_connector_calls_is_empty(tmp_path):
    db = tmp_path / "journal.db"
    _journal(db, [])
    assert status.connector_seen(db) == {}
    assert status.connector_seen(tmp_path / "missing.db") == {}
