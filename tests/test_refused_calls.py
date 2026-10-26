"""Refused calls (server.core.refused): which apps the server turned away, for Status."""

from datetime import datetime, timedelta, timezone

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from server.capture.identity import harness_for_name
from server.core import refused
from server.core.refused import RefusedCallsMiddleware


@pytest.fixture(autouse=True)
def _clean():
    refused.clear()
    yield
    refused.clear()


def _app() -> Starlette:
    async def mcp(request):
        if request.headers.get("authorization") != "Bearer good":
            return JSONResponse({"error": "invalid_token"}, status_code=401)
        return JSONResponse({"ok": True})

    async def token(request):
        return JSONResponse({"error": "invalid_grant"}, status_code=401)

    async def health(request):
        return PlainTextResponse("ok", status_code=401)  # not watched

    app = Starlette(routes=[Route("/mcp", mcp, methods=["POST"]), Route("/token", token, methods=["POST"]),
                            Route("/health", health)])
    app.add_middleware(RefusedCallsMiddleware)
    return app


def test_records_refusals_by_app():
    client = TestClient(_app())
    ua = {"user-agent": "codex-mcp-client/0.164.0-alpha.11.1 (Mac OS 26.0.0; arm64)"}
    assert client.post("/mcp", headers=ua).status_code == 401
    assert client.post("/token", headers=ua).status_code == 401
    assert client.post("/mcp", headers={**ua, "authorization": "Bearer good"}).status_code == 200
    client.get("/health", headers=ua)
    rows = refused.recent()
    assert [(r["harness"], r["count"], r["last_path"], r["last_status"]) for r in rows] == [("codex", 2, "/token", 401)]


def test_no_user_agent_and_unknown_clients():
    client = TestClient(_app())
    client.post("/mcp", headers={"user-agent": ""})
    client.post("/mcp", headers={"user-agent": "Some Tool/2.0"})
    assert {r["harness"] for r in refused.recent()} == {"unknown_mcp_client", "some"}


def test_old_refusals_drop_out():
    then = datetime.now(timezone.utc) - timedelta(hours=25)
    refused.record("Cursor/1.0", "/mcp", 401, now=then)
    assert refused.recent() == []
    assert refused.recent(now=then + timedelta(minutes=1))[0]["harness"] == "cursor"


def test_codex_client_name_is_codex():
    assert harness_for_name("codex-mcp-client") == "codex"
    assert harness_for_name("claude-code") == "claude_code"


def test_accepted_request_after_refusals_is_reported():
    """B21: Codex is refused with its old sign-in, then gets in with the token."""
    client = TestClient(_app())
    ua = {"user-agent": "codex-mcp-client/0.164.0"}
    client.post("/mcp", headers=ua)
    [row] = refused.recent()
    assert row["last_ok_at"] is None
    client.post("/mcp", headers={**ua, "authorization": "Bearer good"})
    [row] = refused.recent()
    assert row["last_ok_at"] >= row["last_at"]
