"""ChatGPT evidence-emission: journal raw conversation turns as SourceEvents.

This is the "rewrite the ChatGPT importer to emit source events" task from
Milestone 2, scoped per docs/adr/0001-four-layer-model.md: it journals
evidence only. server.chatgpt_export_parser's classification logic
(StageBasedMemoryExtractor and friends) is untouched and keeps operating
exactly as it does today, as a separate downstream step — deriving
candidates from journaled evidence, rather than from the raw export file
directly, is Milestone 3 work ("separate capture from consolidation"), not
this module's job.

Reuses server.chatgpt_export_parser.ChatGPTConversationParser's active-path
reconstruction and text-extraction primitives rather than re-implementing
them, so the two code paths cannot silently disagree about which messages
are "active" for a branchy conversation.

event_id scheme deliberately matches the existing candidate provenance
format used by server.chatgpt_export_parser.NativeMemoryCandidate.source_record_ids
("chatgpt:<conv_id>:<msg_id>") exactly — see
ChatGPTConversationParser.validate_candidate_provenance. This means a
candidate's source_record_ids already ARE journal event_ids; Milestone 2's
backfill task (server/importers/backfill.py) and any future
"link candidate to its source event" feature need no translation layer.
"""

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Optional

from server.chatgpt_export_parser import ChatGPTConversationParser
from server.core.models import DatePrecision, SourceEvent, SourceProvenance
from server.journal.identity import compute_content_hash
from server.journal.retention import RetentionPolicy
from server.journal.store import SqliteEventStore

logger = logging.getLogger(__name__)

ROLE_TO_ACTOR_TYPE = {"user": "user", "assistant": "assistant", "system": "system", "tool": "tool"}


def journal_chatgpt_export(
    paths: list[str],
    store: SqliteEventStore,
    retention_policy: Optional[RetentionPolicy] = None,
    allowed_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Parse native ChatGPT conversations-*.json export files and journal
    one SourceEvent per active-path message with real text content.

    Args:
        paths: Absolute paths to conversations-*.json files.
        store: Target event store.
        retention_policy: Defaults to an all-raw policy — Milestone 3 is
            where a real content classifier assigns non-default classes.
        allowed_root: Passed through to ChatGPTConversationParser.validate_file_path
            for the same path-traversal protection the existing importer uses.

    Returns:
        Summary stats: conversations_processed, conversations_skipped_do_not_remember,
        messages_seen, events_journaled (new), events_deduped (already present).
    """
    policy = retention_policy or RetentionPolicy()
    stats = {
        "conversations_processed": 0,
        "conversations_skipped_do_not_remember": 0,
        "messages_seen": 0,
        "messages_skipped_empty": 0,
        "events_journaled": 0,
        "events_deduped": 0,
        "events_excluded_by_retention": 0,
    }

    for path_str in paths:
        path = ChatGPTConversationParser.validate_file_path(path_str, allowed_root=allowed_root)
        with open(path, "r", encoding="utf-8") as f:
            conversations = json.load(f)
        if not isinstance(conversations, list):
            raise ValueError(f"Expected JSON array of conversations in {path.name}, got {type(conversations)}")

        for conv in conversations:
            conv_id = conv.get("id") or conv.get("conversation_id")
            if not conv_id:
                continue

            if conv.get("is_do_not_remember"):
                stats["conversations_skipped_do_not_remember"] += 1
                continue

            mapping = conv.get("mapping") or {}
            current_node_id = conv.get("current_node")
            active_nodes, _discarded, _act, _disc = ChatGPTConversationParser.extract_active_path(mapping, current_node_id)
            stats["conversations_processed"] += 1

            for node in active_nodes:
                msg = node.get("message")
                if not msg:
                    continue
                stats["messages_seen"] += 1

                text = ChatGPTConversationParser.extract_message_text(msg)
                if not text:
                    stats["messages_skipped_empty"] += 1
                    continue

                role = (msg.get("author") or {}).get("role")
                actor_type = ROLE_TO_ACTOR_TYPE.get(role, "system")
                msg_id = msg.get("id") or ""
                create_time = msg.get("create_time")
                observed_at = _unix_to_datetime(create_time)

                content = {"text": text, "role": role}
                event_content = policy.apply(content, content_class="default")
                if event_content is None:
                    stats["events_excluded_by_retention"] += 1
                    continue

                event = SourceEvent(
                    schema_version="1.0",
                    event_id=f"chatgpt:{conv_id}:{msg_id}",
                    event_type="turn.completed",
                    source=SourceProvenance(harness="chatgpt", conversation_id=conv_id, turn_id=msg_id),
                    actor_type=actor_type,
                    observed_at=observed_at,
                    event_date=observed_at,
                    date_precision=DatePrecision.EXACT if create_time is not None else DatePrecision.NONE,
                    content=event_content,
                    content_hash=compute_content_hash(content),
                    metadata={"conversation_title": conv.get("title")},
                )
                inserted = store.append(event)
                stats["events_journaled" if inserted else "events_deduped"] += 1

    return stats


def _unix_to_datetime(create_time: Optional[float]) -> datetime:
    if create_time is None:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromtimestamp(float(create_time), tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return datetime.now(timezone.utc)
