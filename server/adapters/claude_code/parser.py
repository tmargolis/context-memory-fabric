"""Claude Code / Desktop Code-tab transcript line -> canonical SourceEvent (MS4b).

Reads the JSONL transcript format both the standalone Claude Code CLI and
Claude Desktop's Code tab write to `~/.claude/projects/<project-slug>/
<session-uuid>.jsonl` (see docs/plan-active.md's MS4b scope correction —
the parser reads by file shape, not by which binary wrote it). Every line
is JSON with a `type` field.

Keep/skip rule, measured against 8 real transcript files (0.3-8.0MB):

    KEPT   `user`     -- string content is a real user turn; a `tool_result`
                         list item is tool output fed back to the model
                         (bounded per the exit gate below).
           `assistant` -- `text`/`thinking` blocks are real assistant
                         substance (full text, `thinking` often holds a
                         coding session's real reasoning); `tool_use` blocks
                         are bounded per the exit gate below.
    SKIPPED  Everything else. Desktop adds several bridging types absent
             from the plain CLI's schema (`bridge-session`, `ai-title`,
             `atis-latch`, `frame-link`, `pr-link`, `queue-operation`,
             `attachment`, `last-prompt`, `custom-title`, `system`, `mode`,
             `file-history-snapshot`, `file-history-delta`, `agent-name`,
             `artifact-comment-monitor`, `artifact-autoreact-ledger`) --
             these are expected and skipped quietly. A genuinely unknown
             `type` is also skipped, but logged at WARNING once per type
             per process, since it might be a real schema change worth
             knowing about -- never raised, per the plan's parser contract.

Exit gate on "how much of a coding session is worth keeping": full text for
`user`/`assistant text`/`thinking` blocks, bounded 1000-char summaries only
for `tool_result`/`tool_use` blocks (mirrors server.capture.middleware's
own `_RESULT_SUMMARY_MAX_CHARS` convention for MCP-boundary capture).

Secret redaction runs before hashing, reusing server.capture.filters (the
same MS4a machinery, not a second implementation) -- an API key typed into
a Claude Code turn must not reach the journal any more than one passed as
an MCP tool argument.

Harness is hardcoded to "claude_code", never resolved via MCP client_info
-- this adapter has no MCP session to inspect at all (it reads files after
the fact), and hardcoding sidesteps the known-broken Cowork/Code-tab
identity ambiguity in server.capture.identity entirely (see MS6c's
finding that the two can collapse to the same harness bucket over MCP).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any, Optional

from server.capture import filters
from server.core.models import DatePrecision, SourceEvent, SourceProvenance
from server.journal.identity import compute_content_hash

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"
HARNESS = "claude_code"
EVENT_TYPE_TURN = "turn.completed"

# Bounded summary length for tool_result / tool_use blocks -- see module
# docstring's exit-gate answer.
_BLOCK_SUMMARY_MAX_CHARS = 1000

# Types observed in real transcripts that are deliberately not kept (see
# module docstring). Listed explicitly so an addition here is a reviewable
# decision, not silent — and so a truly unrecognized type (not in this set)
# gets a one-time warning instead of disappearing the same way.
KNOWN_SKIPPED_TYPES = frozenset(
    {
        "bridge-session",
        "ai-title",
        "atis-latch",
        "frame-link",
        "pr-link",
        "queue-operation",
        "attachment",
        "last-prompt",
        "custom-title",
        "system",
        "mode",
        "file-history-snapshot",
        "file-history-delta",
        "agent-name",
        "artifact-comment-monitor",
        "artifact-autoreact-ledger",
    }
)

KNOWN_KEPT_TYPES = frozenset({"user", "assistant"})

_warned_unknown_types: set[str] = set()


class ParseStats(dict):
    """Per-file/per-run parse counters, deliberately a plain dict subclass
    so callers (transcript_reader, tests) can just read keys without a
    separate accessor surface.
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


def _extract_user_content(message: dict[str, Any]) -> Optional[dict[str, Any]]:
    raw = message.get("content")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        return {"text": text, "kind": "user_turn"}
    if isinstance(raw, list):
        parts = []
        for block in raw:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                content = block.get("content")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                parts.append({"type": "tool_result", "text": _truncate(text.strip())})
            elif block.get("type") == "text" and block.get("text"):
                parts.append({"type": "text", "text": block["text"]})
        if not parts:
            return None
        joined = " ".join(p["text"] for p in parts if p.get("type") == "text")
        return {"text": joined, "blocks": parts, "kind": "tool_result" if not joined else "user_turn"}
    return None


def _extract_assistant_content(message: dict[str, Any]) -> Optional[dict[str, Any]]:
    raw = message.get("content")
    if not isinstance(raw, list):
        return None
    blocks = []
    text_parts = []
    for block in raw:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and block.get("text"):
            blocks.append({"type": "text", "text": block["text"]})
            text_parts.append(block["text"])
        elif btype == "thinking" and block.get("thinking"):
            blocks.append({"type": "thinking", "text": block["thinking"]})
            text_parts.append(block["thinking"])
        elif btype == "tool_use":
            summary = _truncate(json.dumps(block.get("input", {}), ensure_ascii=False))
            blocks.append({"type": "tool_use", "name": block.get("name"), "input_summary": summary})
    if not blocks:
        return None
    return {"text": "\n\n".join(text_parts), "blocks": blocks, "kind": "assistant_turn"}


def parse_line(
    raw_line: str,
    *,
    session_id: str,
    project_path: Optional[str] = None,
    stats: Optional[ParseStats] = None,
) -> Optional[SourceEvent]:
    """Parse one JSONL line into a SourceEvent, or None if it should be skipped.

    Never raises: any parse or shape failure is counted and logged, not
    propagated -- a single malformed/unexpected line must not abort tailing
    the rest of a live-growing file.
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
        logger.debug("claude_code parser: unparseable line in session %s", session_id)
        return None

    if not isinstance(record, dict):
        stats["lines_unparseable"] += 1
        return None

    rtype = record.get("type")

    if rtype in KNOWN_SKIPPED_TYPES:
        stats["skipped_known"] += 1
        return None

    if rtype not in KNOWN_KEPT_TYPES:
        stats["skipped_unknown"] += 1
        if rtype not in _warned_unknown_types:
            _warned_unknown_types.add(rtype)
            logger.warning("claude_code parser: unrecognized transcript type %r, skipping (session %s)", rtype, session_id)
        return None

    message = record.get("message")
    if not isinstance(message, dict):
        stats["skipped_empty"] += 1
        return None

    if rtype == "user":
        extracted = _extract_user_content(message)
        actor_type = "user"
    else:
        extracted = _extract_assistant_content(message)
        actor_type = "assistant"

    # A pure tool_result turn has empty joined `text` by design (see
    # _extract_user_content) but still carries real bounded-summary content
    # in `blocks` -- only drop when there is truly nothing in either.
    if extracted is None or (not (extracted.get("text") or "").strip() and not extracted.get("blocks")):
        stats["skipped_empty"] += 1
        return None

    turn_uuid = record.get("uuid")
    if not turn_uuid:
        stats["skipped_empty"] += 1
        return None

    timestamp = record.get("timestamp")
    observed_at = _parse_iso(timestamp) or datetime.now(timezone.utc)

    parent_uuid = record.get("parentUuid")
    parent_event_ids = [f"{HARNESS}:{session_id}:{parent_uuid}"] if parent_uuid else []

    # Redact before hashing -- see module docstring.
    redacted_content, redacted_count = filters.redact_secrets_and_count(extracted)

    content_hash = compute_content_hash(redacted_content)
    event_id = f"{HARNESS}:{session_id}:{turn_uuid}"

    event = SourceEvent(
        schema_version=SCHEMA_VERSION,
        event_id=event_id,
        event_type=EVENT_TYPE_TURN,
        source=SourceProvenance(
            harness=HARNESS,
            conversation_id=session_id,
            session_id=session_id,
            turn_id=turn_uuid,
            model=(record.get("message") or {}).get("model") if rtype == "assistant" else None,
        ),
        actor_type=actor_type,
        observed_at=observed_at,
        event_date=observed_at,
        date_precision=DatePrecision.EXACT,
        content=redacted_content,
        content_hash=content_hash,
        parent_event_ids=parent_event_ids,
        metadata={
            "project_path": project_path,
            "entrypoint": record.get("entrypoint"),
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
