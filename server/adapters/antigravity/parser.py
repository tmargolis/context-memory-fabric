"""Antigravity IDE transcript line -> canonical SourceEvent.

Reads the JSONL transcript Antigravity itself writes per conversation to
`<app_data_dir>/brain/<conversation_id>/.system_generated/logs/transcript.jsonl`
(`<app_data_dir>` is `~/.gemini/antigravity` or `~/.gemini/antigravity-ide`
-- see server.adapters.antigravity.transcript_reader). This is the same file
named by the `transcriptPath` field in Antigravity's documented hook payload
(https://antigravity.google/docs/hooks/) -- plain, officially-shaped JSON,
not a reverse-engineered format. Deliberately NOT parsing Antigravity's
separate proprietary per-conversation SQLite/protobuf store
(~/.gemini/antigravity/conversations/*.db) -- that was explicitly ruled out
(the user, 2026-09-24): no parsing of undocumented proprietary binary schemas.

Keep/skip rule, measured against real transcripts (source, type) pairs:

    KEPT   USER_EXPLICIT / USER_INPUT   -- a real user turn.
           MODEL / PLANNER_RESPONSE     -- assistant text and/or tool_calls;
                                           this is the actual substance of a
                                           session.
           MODEL / GENERIC (status=DONE only) -- tool-result / intermediate
                                           model content; RUNNING/ERROR-status
                                           GENERIC rows are transient and
                                           superseded by the eventual DONE
                                           row (or never resolve), so only
                                           DONE is kept to avoid duplicate/
                                           partial content in the journal.
    SKIPPED  SYSTEM / SYSTEM_MESSAGE, SYSTEM / CHECKPOINT, SYSTEM / ERROR_MESSAGE
             -- session bookkeeping, not conversation substance. A genuinely
             unrecognized (source, type) pair is also skipped, but logged at
             WARNING once per pair per process, since Antigravity adding a
             new step type is a real schema change worth knowing about --
             never raised, per the parser contract (a single malformed or
             unexpected line must not abort tailing the rest of a
             live-growing file).

Exit gate mirrors server.adapters.claude_code.parser: full text for user/
assistant content, bounded 1000-char summaries for tool_call args (matches
server.capture.middleware's own _RESULT_SUMMARY_MAX_CHARS convention).

Known quirk: some tool_calls[].args string values in transcript.jsonl are
double-JSON-encoded (e.g. "AbsolutePath": "\"/Users/<user>/...\"") while the
sibling transcript_full.jsonl does not have this quirk. Handled defensively
here (strip one layer of quoting) rather than switching source files, since
transcript.jsonl is the file the hook payload's transcriptPath actually
names.

Secret redaction runs before hashing, reusing server.capture.filters -- same
MS4a machinery as every other adapter, not a second implementation.

Harness is hardcoded to "antigravity" for both app_data_dir roots (matches
the harness slug server.capture.identity.resolve_harness already assigns to
Antigravity's MCP client_info) -- this adapter has no MCP session to inspect
at all (it reads files after the fact); which root a conversation came from
is recorded in metadata.app_data_dir instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import re
from typing import Any, Optional

from server.capture import filters
from server.core.models import DatePrecision, SourceEvent, SourceProvenance
from server.journal.identity import compute_content_hash, compute_event_id

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"
HARNESS = "antigravity"
EVENT_TYPE_TURN = "turn.completed"

# Bounded summary length for tool_call args -- see module docstring's exit
# gate answer.
_BLOCK_SUMMARY_MAX_CHARS = 1000

KNOWN_SKIPPED_PAIRS = frozenset(
    {
        ("SYSTEM", "SYSTEM_MESSAGE"),
        ("SYSTEM", "CHECKPOINT"),
        ("SYSTEM", "ERROR_MESSAGE"),
    }
)

KNOWN_KEPT_PAIRS = frozenset(
    {
        ("USER_EXPLICIT", "USER_INPUT"),
        ("MODEL", "PLANNER_RESPONSE"),
        ("MODEL", "GENERIC"),
    }
)

_warned_unknown_pairs: set[tuple[Optional[str], Optional[str]]] = set()

_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)


class ParseStats(dict):
    """Per-file/per-run parse counters -- plain dict subclass, same shape as
    server.adapters.claude_code.parser.ParseStats, so callers can just read
    keys without a separate accessor surface.
    """

    def __init__(self) -> None:
        super().__init__(
            lines_seen=0,
            lines_unparseable=0,
            kept=0,
            skipped_known=0,
            skipped_unknown=0,
            skipped_empty=0,
        )


def _truncate(text: str) -> str:
    if len(text) > _BLOCK_SUMMARY_MAX_CHARS:
        return text[:_BLOCK_SUMMARY_MAX_CHARS] + "...[truncated]"
    return text


def _unwrap_double_encoded(value: Any) -> Any:
    """Undo the transcript.jsonl double-JSON-encoding quirk on a single
    string value: '"foo"' -> 'foo'. Any value that isn't a once-more-quoted
    string is returned unchanged -- this is best-effort cosmetic cleanup,
    never load-bearing for correctness.
    """
    if isinstance(value, str) and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            unwrapped = json.loads(value)
            if isinstance(unwrapped, str):
                return unwrapped
        except (json.JSONDecodeError, ValueError):
            pass
    return value


def _extract_user_input(record: dict[str, Any]) -> Optional[dict[str, Any]]:
    raw = record.get("content")
    if not isinstance(raw, str) or not raw.strip():
        return None
    match = _USER_REQUEST_RE.search(raw)
    text = match.group(1).strip() if match else raw.strip()
    if not text:
        return None
    return {"text": text, "kind": "user_turn"}


def _extract_planner_response(record: dict[str, Any]) -> Optional[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    text_parts: list[str] = []

    content = record.get("content")
    if isinstance(content, str) and content.strip():
        blocks.append({"type": "text", "text": content})
        text_parts.append(content)

    tool_calls = record.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            name = call.get("name")
            args = call.get("args")
            cleaned_args = (
                {k: _unwrap_double_encoded(v) for k, v in args.items()} if isinstance(args, dict) else args
            )
            summary = _truncate(json.dumps(cleaned_args, ensure_ascii=False))
            blocks.append({"type": "tool_call", "name": name, "args_summary": summary})

    if not blocks:
        return None
    return {"text": "\n\n".join(text_parts), "blocks": blocks, "kind": "assistant_turn"}


def _extract_generic(record: dict[str, Any]) -> Optional[dict[str, Any]]:
    if record.get("status") != "DONE":
        return None
    content = record.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return {"text": _truncate(content.strip()), "kind": "tool_result"}


def parse_line(
    raw_line: str,
    *,
    conversation_id: str,
    project: Optional[str] = None,
    app_data_dir: Optional[str] = None,
    stats: Optional[ParseStats] = None,
) -> Optional[SourceEvent]:
    """Parse one transcript.jsonl line into a SourceEvent, or None if it
    should be skipped. Never raises -- any parse or shape failure is counted
    and logged, not propagated.
    """
    stats = stats if stats is not None else ParseStats()
    stats["lines_seen"] += 1

    line = raw_line.strip()
    if not line:
        stats["skipped_empty"] += 1
        return None

    try:
        record = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        stats["lines_unparseable"] += 1
        logger.debug("antigravity parser: unparseable line in conversation %s", conversation_id)
        return None

    if not isinstance(record, dict):
        stats["lines_unparseable"] += 1
        return None

    source = record.get("source")
    rtype = record.get("type")
    pair = (source, rtype)

    if pair in KNOWN_SKIPPED_PAIRS:
        stats["skipped_known"] += 1
        return None

    if pair not in KNOWN_KEPT_PAIRS:
        stats["skipped_unknown"] += 1
        if pair not in _warned_unknown_pairs:
            _warned_unknown_pairs.add(pair)
            logger.warning(
                "antigravity parser: unrecognized transcript (source=%r, type=%r), skipping (conversation %s)",
                source,
                rtype,
                conversation_id,
            )
        return None

    if pair == ("USER_EXPLICIT", "USER_INPUT"):
        extracted = _extract_user_input(record)
        actor_type = "user"
    elif pair == ("MODEL", "PLANNER_RESPONSE"):
        extracted = _extract_planner_response(record)
        actor_type = "assistant"
    else:  # ("MODEL", "GENERIC")
        extracted = _extract_generic(record)
        actor_type = "assistant"

    if extracted is None or (not (extracted.get("text") or "").strip() and not extracted.get("blocks")):
        stats["skipped_empty"] += 1
        return None

    step_index = record.get("step_index")
    if step_index is None:
        stats["skipped_empty"] += 1
        return None
    turn_id = str(step_index)

    observed_at = _parse_iso(record.get("created_at")) or datetime.now(timezone.utc)

    # Redact before hashing -- see module docstring.
    redacted_content, redacted_count = filters.redact_secrets_and_count(extracted)

    content_hash = compute_content_hash(redacted_content)
    event_id = compute_event_id(
        harness=HARNESS,
        content_hash=content_hash,
        conversation_id=conversation_id,
        turn_id=turn_id,
    )

    event = SourceEvent(
        schema_version=SCHEMA_VERSION,
        event_id=event_id,
        event_type=EVENT_TYPE_TURN,
        source=SourceProvenance(
            harness=HARNESS,
            conversation_id=conversation_id,
            session_id=conversation_id,
            turn_id=turn_id,
            model=record.get("model") if actor_type == "assistant" else None,
        ),
        actor_type=actor_type,
        observed_at=observed_at,
        event_date=observed_at,
        date_precision=DatePrecision.EXACT,
        content=redacted_content,
        content_hash=content_hash,
        metadata={
            "project": project,
            "app_data_dir": app_data_dir,
            "redacted_field_count": redacted_count,
            "content_kind": redacted_content.get("kind"),
        },
    )
    stats["kept"] += 1
    return event


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
