"""Bearer-token auth for the network MCP transports (streamable-http, sse).

stdio never reaches this middleware -- it has no HTTP layer, so a client
connected via stdio is trusted by process ownership alone, same as today.
This module exists because streamable-http/sse otherwise accept any request
that reaches the port with zero auth: once the server is reachable over a
network (including a Tailscale Funnel -- see docs/CLIENTS.md), anyone who
can send it a request can call every tool, including
remember/edit_memory/reconcile_memories/import_chatgpt_exports/
promote_auto_accepted_memories -- full read/write access to the memory graph.

Deliberately not the mcp SDK's OAuth machinery (mcp.server.auth): that
AuthSettings requires a real issuer_url/resource_server_url for an OAuth
protected-resource-metadata flow this deployment has no authorization
server for. A single shared-secret bearer token is what "pass an API key
from the client" means here.
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
        if scheme.lower() != "bearer" or not secrets.compare_digest(presented, self._token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def get_configured_auth_token() -> Optional[str]:
    """Read the configured auth token, or None if unset/blank."""
    token = os.getenv(AUTH_TOKEN_ENV_VAR)
    return token.strip() if token and token.strip() else None
