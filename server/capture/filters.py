"""Secret redaction and tool allow/deny filtering for MCP-boundary capture.

Redaction runs before anything is hashed or journaled — an API key that
never reaches compute_content_hash()/the events table cannot leak through
the journal, replay, or any downstream consolidation output. This is the
concrete implementation of MS0.5's live fixture: GEMINI_API_KEY-shaped
strings (Google API keys: "AIza" + 35 chars) must never reach the journal.

Value-shape redaction (regex over string content) reuses
server.journal.retention.redact_secret_patterns — the same pattern list
MS2 already maintains for importer content, rather than a second list here
that could drift from it (see that function's docstring). This module adds
only what retention.py doesn't cover: a key-name heuristic, since MCP tool
arguments frequently carry named fields like `api_key` whose value might
not match any known provider's format.
"""

from __future__ import annotations

import os
import re
from typing import Any

from server.journal.retention import redact_secret_patterns

REDACTED = "[REDACTED]"

# Argument/dict-key names that indicate the value is a credential regardless
# of whether its shape matches a pattern retention.py recognizes — catches
# secrets in formats not enumerated there (a new provider's key format, a
# raw password).
_SECRET_KEY_NAME_PATTERN = re.compile(
    r"(api[_-]?key|secret|password|passwd|token|credential|auth[_-]?header|bearer)", re.IGNORECASE
)


def redact_secrets(value: Any) -> Any:
    """Recursively redact secret-shaped values in a JSON-like structure.

    Dicts: a value is fully redacted if its key name matches the
    credential-name heuristic; otherwise it's walked recursively and its
    string leaves are passed through retention.py's pattern-based scrub
    regardless of key name (catches a credential under an innocuous key).
    Lists and nested dicts are walked recursively. Non-string, non-dict,
    non-list values pass through unchanged.
    """
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            if isinstance(v, str) and _SECRET_KEY_NAME_PATTERN.search(str(k)):
                result[k] = REDACTED
            else:
                result[k] = redact_secrets(v)
        return result
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    if isinstance(value, str):
        return redact_secret_patterns(value)
    return value


def redact_secrets_and_count(value: Any) -> tuple[Any, int]:
    """redact_secrets() plus a count of values actually changed, for health reporting."""
    count = 0

    def _walk(v: Any) -> Any:
        nonlocal count
        if isinstance(v, dict):
            result = {}
            for k, vv in v.items():
                if isinstance(vv, str) and _SECRET_KEY_NAME_PATTERN.search(str(k)):
                    count += 1
                    result[k] = REDACTED
                else:
                    result[k] = _walk(vv)
            return result
        if isinstance(v, list):
            return [_walk(vv) for vv in v]
        if isinstance(v, str):
            redacted = redact_secret_patterns(v)
            if redacted != v:
                count += 1
            return redacted
        return v

    return _walk(value), count


# Tools with their own dedicated Milestone 2 importer already producing
# higher-fidelity source events than generic MCP-call capture could
# (structured branch reconstruction, per-message provenance, etc.).
# Capturing them again through this generic path would journal the same
# content twice under a lower-fidelity shape, so they're excluded by
# default. Overridable via CMF_CAPTURE_DENY_TOOLS (comma-separated,
# replaces the default list entirely) for cases where that tradeoff should
# change.
DEFAULT_DENIED_TOOLS = {"import_memories", "import_chatgpt_exports"}


def denied_tools() -> set[str]:
    env_value = os.getenv("CMF_CAPTURE_DENY_TOOLS")
    if env_value is not None:
        return {t.strip() for t in env_value.split(",") if t.strip()}
    return set(DEFAULT_DENIED_TOOLS)


def denied_clients() -> set[str]:
    """Harness slugs to never capture, e.g. during local development.

    Empty by default; set CMF_CAPTURE_DENY_CLIENTS (comma-separated harness
    slugs) to exclude specific clients.
    """
    env_value = os.getenv("CMF_CAPTURE_DENY_CLIENTS")
    if env_value is not None:
        return {c.strip() for c in env_value.split(",") if c.strip()}
    return set()


def should_capture(tool_name: str, harness: str) -> bool:
    return tool_name not in denied_tools() and harness not in denied_clients()
