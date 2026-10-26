"""Refused calls: which AI apps the server turned away, for CMF management's Status.

A 401 never reaches a tool, so nothing else records it: an app whose sign-in
lapsed (a Codex OAuth login from before the journal moved, 2026-10-25) looks
connected from its config file while every call it makes is refused. This
counts refused requests to the MCP endpoint and the OAuth token endpoint by
app, named from the User-Agent the way MCP-boundary capture names clients
(server.capture.identity). In memory only, since the server started: no
content, no tokens, no addresses.

It also notes each app's latest accepted MCP request (B21): Codex tries a
stale OAuth sign-in before its token on every connect, and those refusals are
not a lockout when the same app gets in right after.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from starlette.types import ASGIApp, Message, Receive, Scope, Send

WINDOW = timedelta(hours=24)
_WATCHED = ("/mcp", "/token")

_lock = threading.Lock()
_refused: dict[str, dict[str, Any]] = {}
_accepted: dict[str, str] = {}


def _client_name(user_agent: str) -> Optional[str]:
    """The client's own name: the User-Agent's first product token ("codex-mcp-client/0.164" -> "codex-mcp-client")."""
    first = user_agent.strip().split(" ", 1)[0] if user_agent.strip() else ""
    return first.split("/", 1)[0] or None


def record(user_agent: str, path: str, status: int, now: Optional[datetime] = None) -> None:
    from server.capture.identity import harness_for_name

    harness = harness_for_name(_client_name(user_agent))
    at = (now or datetime.now(timezone.utc)).isoformat()
    with _lock:
        entry = _refused.setdefault(harness, {"harness": harness, "count": 0, "first_at": at})
        entry.update(count=entry["count"] + 1, last_at=at, last_path=path, last_status=status)


def record_accepted(user_agent: str, now: Optional[datetime] = None) -> None:
    from server.capture.identity import harness_for_name

    harness = harness_for_name(_client_name(user_agent))
    with _lock:
        _accepted[harness] = (now or datetime.now(timezone.utc)).isoformat()


def recent(now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Apps refused in the last 24 h, latest first, each with its latest accepted MCP request (or None)."""
    since = ((now or datetime.now(timezone.utc)) - WINDOW).isoformat()
    with _lock:
        rows = [{**e, "last_ok_at": _accepted.get(e["harness"])} for e in _refused.values() if e["last_at"] >= since]
    return sorted(rows, key=lambda e: e["last_at"], reverse=True)


def clear() -> None:
    with _lock:
        _refused.clear()
        _accepted.clear()


class RefusedCallsMiddleware:
    """Records 401/403 answers on /mcp and /token, and accepted /mcp requests; passes everything through unchanged."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not path.rstrip("/").endswith(_WATCHED):
            await self.app(scope, receive, send)
            return

        async def watch(message: Message) -> None:
            if message["type"] == "http.response.start":
                status = message["status"]
                if status in (401, 403) or (200 <= status < 300 and path.rstrip("/").endswith("/mcp")):
                    agent = dict(scope.get("headers") or []).get(b"user-agent", b"").decode("latin-1")
                    if status in (401, 403):
                        record(agent, path, status)
                    else:
                        record_accepted(agent)
            await send(message)

        await self.app(scope, receive, watch)
