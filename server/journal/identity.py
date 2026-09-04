"""Event identity: content hashing and event_id derivation.

The dedup contract (see docs/schemas/source-event-1.0.json and Milestone 2's
acceptance test "importing the same export twice produces zero new events")
rests on event_id being the sole idempotency key, not content_hash. Two
events can legitimately share identical text (e.g. an assistant's routine
"Understood.") in different conversations — hashing raw content alone would
wrongly collapse them into one event. content_hash is therefore only ever
used to construct event_id when no native stable identifier exists at all.
"""

import hashlib
import json
import unicodedata
from typing import Any, Optional


def compute_content_hash(content: dict[str, Any]) -> str:
    """Deterministic sha256 over a canonicalized JSON form of `content`.

    Canonicalization: NFC Unicode normalization + trailing/leading whitespace
    stripped on every string value (recursively), keys sorted, no
    insignificant whitespace in the JSON separators. This makes the hash
    stable across near-miss variations that do not change meaning —
    re-exporting the same conversation should not silently mint new
    content_hash values because a platform changed trailing-space
    formatting.

    Returns:
        "sha256:<64 hex chars>", matching the pattern in
        docs/schemas/source-event-1.0.json.
    """
    normalized = _normalize(content)
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value.strip())
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def compute_event_id(
    harness: str,
    content_hash: str,
    conversation_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    namespace: Optional[str] = None,
) -> str:
    """Derive a stable event_id.

    Preferred form (native stable ID available): "<harness>:<conversation_id>:<turn_id>".
    Fallback form (no native ID): "<harness>:<content_hash>" (optionally
    "<harness>:<namespace>:<content_hash>" when a namespace disambiguates,
    e.g. "chatgpt:backfill:..." for reconstructed events or
    "chatgpt:sha256:..." — see docs/schemas/source-event-1.0-examples.md
    examples 4 and 5).

    Args:
        harness: Source harness slug, e.g. "chatgpt", "claude".
        content_hash: Output of compute_content_hash(content) — always
            required, even when a native ID is used, so the fallback path
            and the caller share one hashing code path.
        conversation_id: Native conversation identifier, if any.
        turn_id: Native per-turn/message identifier, if any. Only used when
            conversation_id is also present — a turn_id without a
            conversation_id is not trusted as globally unique on its own.
        namespace: Optional disambiguating segment for the fallback form.
    """
    if conversation_id and turn_id:
        return f"{harness}:{conversation_id}:{turn_id}"

    hash_value = content_hash.split(":", 1)[-1] if ":" in content_hash else content_hash
    if namespace:
        return f"{harness}:{namespace}:{hash_value}"
    return f"{harness}:sha256:{hash_value}"
