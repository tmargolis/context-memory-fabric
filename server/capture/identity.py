"""Harness and session identity for MCP-boundary capture.

Two identity problems, both without a clean answer from the MCP spec
itself:

1. Harness identity: `client_info` (name/version) is free-form text each
   client author chooses. There is no registry of canonical client names,
   so this module normalizes known clients by substring match and falls
   back to a slugified raw name for anything unrecognized — never raising
   or dropping capture just because a client is new.

2. Session identity: MCP has no notion of a "conversation." A `session_id`
   exists at the transport level for streamable-http/SSE, but is `None` on
   stdio (mcp/server/context.py's own docstring) — and stdio is exactly the
   transport Claude Desktop uses, the primary MS4a target. CMF therefore
   synthesizes its own session identity: a UUID4 minted the first time a
   given `ServerSession` Python object is observed, cached against that
   object's identity for the life of the connection (a WeakKeyDictionary,
   so a session is not kept alive by this cache once its connection
   closes). This is CMF-local identity, not a protocol-level session id —
   documented in docs/adapters/mcp-boundary.md.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Optional
from weakref import WeakKeyDictionary

# Known-client substring matches, checked in order against the lowercased
# client_info.name. First match wins. Add entries here as new clients are
# observed in practice rather than guessing every possible client name
# up front — a client that doesn't match falls through to _fallback_slug,
# never to an error.
_KNOWN_HARNESS_PATTERNS: list[tuple[str, str]] = [
    (r"claude.*desktop", "claude_desktop"),
    (r"claude.*code", "claude_code"),
    (r"claude.*ai", "claude_desktop"),  # observed client_info.name variant
    (r"chatgpt|openai", "chatgpt"),
    (r"cursor", "cursor"),
    (r"antigravity", "antigravity"),
    (r"windsurf", "windsurf"),
    (r"cline", "cline"),
]


def resolve_harness(client_info: Optional[Any]) -> str:
    """Normalize `client_info` (an mcp_types.Implementation, or None) to a harness slug.

    `client_info` is whatever the connecting client declared at handshake
    (`ctx.session.client_params.client_info` in a request handler); it is
    optional per the MCP spec (client info is not required — see
    mcp/server/connection.py's `Connection.from_envelope` docstring), so
    this function tolerates `None` and any object missing a `.name`.
    """
    name = getattr(client_info, "name", None)
    if not name or not str(name).strip():
        return "unknown_mcp_client"

    lowered = str(name).strip().lower()
    for pattern, slug in _KNOWN_HARNESS_PATTERNS:
        if re.search(pattern, lowered):
            return slug

    return _fallback_slug(str(name))


def _fallback_slug(raw_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", raw_name.strip().lower()).strip("_")
    return slug or "unknown_mcp_client"


def client_version(client_info: Optional[Any]) -> Optional[str]:
    version = getattr(client_info, "version", None)
    return str(version) if version else None


# Session-id cache: keyed by the ServerSession object itself so it is
# automatically evicted once that session's connection is garbage
# collected. One process-local synthesized id per connection, reused for
# every request on that connection.
_SESSION_IDS: "WeakKeyDictionary[Any, str]" = WeakKeyDictionary()


def resolve_session_id(session: Any) -> str:
    """A stable, process-local session identity for `session` (a ServerSession).

    Prefers the transport's real session id when the connection actually
    has one (streamable-http/SSE); synthesizes and caches a UUID4 the first
    time a given session object is seen otherwise (stdio, and any future
    transport with no native session id). Reaching into `session._connection`
    is a private-attribute access tied to mcp==2.1.1's internal shape —
    guarded by getattr/hasattr so an SDK upgrade that changes this layout
    degrades to the synthesized id rather than raising.
    """
    connection = getattr(session, "_connection", None)
    native_session_id = getattr(connection, "session_id", None) if connection is not None else None
    if native_session_id:
        return f"native:{native_session_id}"

    try:
        cached = _SESSION_IDS.get(session)
        if cached is not None:
            return cached
    except TypeError:
        # session isn't weak-referenceable (real ServerSession objects are;
        # this guards test doubles and any future session type that isn't,
        # since capture must never raise regardless). No caching possible
        # for this object — every call synthesizes a fresh id.
        return f"cmf:{uuid.uuid4().hex}"

    synthesized = f"cmf:{uuid.uuid4().hex}"
    try:
        _SESSION_IDS[session] = synthesized
    except TypeError:
        pass
    return synthesized
