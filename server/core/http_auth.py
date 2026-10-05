"""Bearer-token auth for the network MCP transports (streamable-http, sse).

stdio never reaches this middleware -- it has no HTTP layer, so a client
connected via stdio is trusted by process ownership alone, same as today.
This module exists because streamable-http/sse otherwise accept any request
that reaches the port with zero auth: once the server is reachable over a
network (including a Tailscale Funnel -- see docs/CLIENTS.md), anyone who
can send it a request can call every tool, including
remember/edit_memory/reconcile_memories/import_chatgpt_exports/
promote_auto_accepted_memories -- full read/write access to the memory graph.

This is the static shared-secret fallback, for direct/manual HTTP access.
The client apps (Claude Desktop, ChatGPT, Gemini) connect through the OAuth
authorization server in server.core.oauth_provider instead, since none of
their connector UIs accept a static bearer token. The two are mutually
exclusive on the network transport: when OAuth is configured
(CMF_MCP_ISSUER_URL), server/mcp.py ignores CMF_MCP_AUTH_TOKEN and this
middleware isn't installed.
"""

import os
import secrets
from typing import Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

AUTH_TOKEN_ENV_VAR = "CMF_MCP_AUTH_TOKEN"


class BearerTokenAuthMiddleware(BaseHTTPMiddleware):
    """Rejects any request that doesn't carry `Authorization: Bearer <token>`."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        # Compare as bytes: compare_digest raises TypeError on non-ASCII str.
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            presented.encode("utf-8"), self._token.encode("utf-8")
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def get_configured_auth_token() -> Optional[str]:
    """Read the configured auth token, or None if unset/blank."""
    token = os.getenv(AUTH_TOKEN_ENV_VAR)
    return token.strip() if token and token.strip() else None
