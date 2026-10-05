"""Codex transcript line -> canonical SourceEvent (MS4d).

Reads the JSONL transcript Codex writes per conversation to
`~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<timestamp>-<uuid>.jsonl`.

Line types observed and keep/skip rules:

    KEPT
        `response_item` (payload.type="message", role="user")
            Real user turn. Injected developer/environment blocks
            (<environment_context>, <recommended_plugins>) are stripped.
        `response_item` (payload.type="message", role="assistant")
            Assistant text (output_text).
        `response_item` (payload.type in ("custom_tool_call", "function_call"))
            Tool call invocations (bounded 1000-char input summary).
        `response_item` (payload.type in ("custom_tool_call_output", "function_call_output"))
            Tool results (bounded 1000-char output summary).

    SKIPPED
        `response_item` (payload.type="message", role="developer")
            System / role instructions.
        `response_item` (payload.type="reasoning")
            Encrypted reasoning / internal thinking blocks.
        `session_meta`, `turn_context`
            Session and turn context metadata (used to resolve session identity
            and project cwd; skipped as turn events).
        `world_state`, `token_usage_record`, `event_msg`
            Lifecycle and bookkeeping events (item_completed, task_started,
            task_complete, token counts, permission snapshots).

Secret redaction runs before hashing, reusing server.capture.filters.
Harness is hardcoded to "codex".
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
HARNESS = "codex"
EVENT_TYPE_TURN = "turn.completed"

_BLOCK_SUMMARY_MAX_CHARS = 1000

# Regex patterns for stripping injected system/environment context from user turns
_ENV_CONTEXT_RE = re.compile(r"<environment_context>.*?</environment_context>", re.DOTALL)
_REC_PLUGINS_RE = re.compile(r"<recommended_plugins>.*?</recommended_plugins>", re.DOTALL)

KNOWN_SKIPPED_LINE_TYPES = frozenset({
    "session_meta",
    "turn_context",
    "world_state",
    "token_usage_record",
    "event_msg",
})

KNOWN_SKIPPED_PAYLOAD_TYPES = frozenset({
    "reasoning",
    "item_completed",
    "task_started",
    "task_complete",
    "thread_settings_applied",
    "token_count",
    "user_message",
    "agent_message",
    "agent_reasoning",
})

_warned_unknown_types: set[str] = set()


class ParseStats(dict):
    """Per-file/per-run parse counters."""

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


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def extract_session_meta(raw_line: str) -> Optional[dict[str, Any]]:
    """Extract metadata from a session_meta or turn_context line if present."""
    line = raw_line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    ltype = data.get("type")
    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        return None

    if ltype == "session_meta":
        sess_id = payload.get("session_id")
        item_id = payload.get("id")
        conv_id = item_id or sess_id
        return {
            "session_id": sess_id or item_id,
            "id": item_id,
            "conversation_id": conv_id,
            "parent_thread_id": payload.get("parent_thread_id"),
            "cwd": payload.get("cwd"),
            "workspace_roots": payload.get("runtime_workspace_roots") or [],
            "originator": payload.get("originator"),
            "cli_version": payload.get("cli_version"),
            "timestamp": payload.get("timestamp") or data.get("timestamp"),
            "git": payload.get("git"),
            "source": payload.get("source"),
        }
    if ltype == "turn_context":
        return {
            "turn_id": payload.get("turn_id"),
            "cwd": payload.get("cwd"),
            "workspace_roots": payload.get("workspace_roots") or [],
            "timestamp": data.get("timestamp"),
        }
    return None


def _clean_user_text(raw_text: str) -> str:
    """Strip injected environment/plugin XML blocks from user input."""
    text = _ENV_CONTEXT_RE.sub("", raw_text)
    text = _REC_PLUGINS_RE.sub("", text)
    return text.strip()


def _extract_user_message(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    content = payload.get("content", [])
    if isinstance(content, str):
        text = _clean_user_text(content)
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "input_text":
                t = item.get("text", "")
                if t:
                    parts.append(t)
            elif isinstance(item, str):
                parts.append(item)
        text = _clean_user_text("\n\n".join(parts))
    else:
        return None

    if not text:
        return None
    return {"text": text, "kind": "user_turn"}


def _extract_assistant_message(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    content = payload.get("content", [])
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "output_text":
                t = item.get("text", "")
                if t:
                    parts.append(t)
            elif isinstance(item, str):
                parts.append(item)
        text = "\n\n".join(parts).strip()
    else:
        return None

    if not text:
        return None
    return {"text": text, "kind": "assistant_turn"}


def _extract_tool_call(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    name = payload.get("name") or "unknown_tool"
    call_id = payload.get("call_id") or payload.get("id") or ""
    raw_input = payload.get("input", "")
    if isinstance(raw_input, (dict, list)):
        input_str = json.dumps(raw_input, ensure_ascii=False)
    else:
        input_str = str(raw_input)

    return {
        "text": f"Tool call: {name}",
        "kind": "tool_use",
        "name": name,
        "call_id": call_id,
        "input_summary": _truncate(input_str),
    }


def _extract_tool_output(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    call_id = payload.get("call_id") or payload.get("id") or ""
    output_val = payload.get("output", "")
    if isinstance(output_val, list):
        parts = []
        for item in output_val:
            if isinstance(item, dict) and "text" in item:
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        output_str = "\n".join(parts)
    elif isinstance(output_val, dict):
        output_str = json.dumps(output_val, ensure_ascii=False)
    else:
        output_str = str(output_val)

    return {
        "text": f"Tool result for {call_id}".strip(),
        "kind": "tool_result",
        "call_id": call_id,
        "output_summary": _truncate(output_str),
    }


def parse_line(
    raw_line: str,
    *,
    conversation_id: str,
    project: Optional[str] = None,
    stats: Optional[ParseStats] = None,
    session_id: Optional[str] = None,
    parent_conversation_id: Optional[str] = None,
) -> Optional[SourceEvent]:
    """Parse one Codex rollout JSONL line into a SourceEvent, or None if skipped.

    Never raises on invalid or unrecognized content.
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
        logger.debug("codex parser: unparseable line in conversation %s", conversation_id)
        return None

    if not isinstance(record, dict):
        stats["lines_unparseable"] += 1
        return None

    ltype = record.get("type")
    if ltype in KNOWN_SKIPPED_LINE_TYPES:
        stats["skipped_known"] += 1
        return None

    if ltype != "response_item":
        stats["skipped_unknown"] += 1
        if ltype not in _warned_unknown_types:
            _warned_unknown_types.add(str(ltype))
            logger.warning(
                "codex parser: unrecognized line type %r, skipping (conversation %s)",
                ltype,
                conversation_id,
            )
        return None

    payload = record.get("payload")
    if not isinstance(payload, dict):
        stats["skipped_empty"] += 1
        return None

    ptype = payload.get("type")
    if ptype in KNOWN_SKIPPED_PAYLOAD_TYPES:
        stats["skipped_known"] += 1
        return None

    extracted: Optional[dict[str, Any]] = None
    actor_type: str = "assistant"

    if ptype == "message":
        role = payload.get("role")
        if role == "developer":
            stats["skipped_known"] += 1
            return None
        elif role == "user":
            extracted = _extract_user_message(payload)
            actor_type = "user"
        elif role == "assistant":
            extracted = _extract_assistant_message(payload)
            actor_type = "assistant"
        else:
            stats["skipped_unknown"] += 1
            return None
    elif ptype in ("custom_tool_call", "function_call"):
        extracted = _extract_tool_call(payload)
        actor_type = "assistant"
    elif ptype in ("custom_tool_call_output", "function_call_output"):
        extracted = _extract_tool_output(payload)
        actor_type = "assistant"
    else:
        stats["skipped_unknown"] += 1
        if ptype not in _warned_unknown_types:
            _warned_unknown_types.add(str(ptype))
            logger.warning(
                "codex parser: unrecognized payload type %r, skipping (conversation %s)",
                ptype,
                conversation_id,
            )
        return None

    if extracted is None:
        stats["skipped_empty"] += 1
        return None

    # Derive unique turn identity
    ordinal = record.get("ordinal")
    item_id = payload.get("id") or payload.get("call_id")
    if ordinal is not None:
        turn_id = str(ordinal)
    elif item_id:
        turn_id = str(item_id)
    else:
        stats["skipped_empty"] += 1
        return None

    observed_at = _parse_iso(record.get("timestamp")) or datetime.now(timezone.utc)

    # Redact before hashing/persistence
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
            session_id=session_id or conversation_id,
            turn_id=turn_id,
            model=record.get("model"),
        ),
        actor_type=actor_type,
        observed_at=observed_at,
        event_date=observed_at,
        date_precision=DatePrecision.EXACT,
        content=redacted_content,
        content_hash=content_hash,
        metadata={
            "project": project,
            "ordinal": ordinal,
            "item_id": item_id,
            "redacted_field_count": redacted_count,
            "content_kind": redacted_content.get("kind"),
            **({"parent_conversation_id": parent_conversation_id} if parent_conversation_id else {}),
            **({"client_authored": record["metadata"]["client_authored"]} if isinstance(record.get("metadata"), dict) and "client_authored" in record["metadata"] else {}),
        },
    )
    stats["kept"] += 1
    return event
