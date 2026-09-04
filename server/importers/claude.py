"""Claude evidence-emission: journal conversations.json + projects/*.json
turns and snapshots as SourceEvents.

Scope matches server.importers.chatgpt: evidence only, no classification —
see that module's docstring and docs/adr/0001-four-layer-model.md.

Branch reconstruction: Claude's export encodes a full message tree via
`parent_message_uuid` (root's parent is the sentinel
"00000000-0000-4000-8000-000000000000"), and roughly 1% of real messages
are duplicate-parented (branches — regenerated responses, edited prompts).
Unlike ChatGPT's export, there is no explicit "current_node"/active-branch
pointer at the conversation level. This module's heuristic: the active leaf
is the leaf message (a message no other message names as its parent) with
the latest `created_at` in the conversation; the active path is that leaf
walked back to the root via `parent_message_uuid`. This is a documented
approximation, not a guarantee — a user who abandoned a later edit to
return to an earlier branch would be misread. Flagged here rather than
silently assumed correct.

Project snapshots are treated as a mutable entity: event_id incorporates
the project's `updated_at`, not just its stable uuid, because re-importing
the *same* project after it was edited must produce a new distinct event
(see docs/schemas/source-event-1.0-examples.md, example 3), not a
dedup-skipped duplicate of stale content.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import json
import zipfile

from server.core.models import DatePrecision, SourceEvent, SourceProvenance
from server.journal.identity import compute_content_hash
from server.journal.retention import RetentionPolicy
from server.journal.store import SqliteEventStore

ROOT_SENTINEL = "00000000-0000-4000-8000-000000000000"
SENDER_TO_ACTOR_TYPE = {"human": "user", "assistant": "assistant"}


def journal_claude_export(
    conversations_zip: Optional[Path],
    projects_zip: Optional[Path],
    store: SqliteEventStore,
    retention_policy: Optional[RetentionPolicy] = None,
) -> dict[str, Any]:
    """Journal one SourceEvent per active-path Claude conversation turn
    and one per project snapshot. Either zip may be omitted (None) to
    journal just the other category.
    """
    policy = retention_policy or RetentionPolicy()
    stats = {
        "conversations_processed": 0,
        "projects_processed": 0,
        "messages_seen": 0,
        "messages_skipped_empty": 0,
        "events_journaled": 0,
        "events_deduped": 0,
        "events_excluded_by_retention": 0,
    }

    if conversations_zip is not None:
        _journal_conversations(Path(conversations_zip), store, policy, stats)
    if projects_zip is not None:
        _journal_projects(Path(projects_zip), store, policy, stats)

    return stats


def _journal_conversations(zip_path: Path, store: SqliteEventStore, policy: RetentionPolicy, stats: dict[str, Any]) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        # Claude's export names the single member "conversations.json"; find
        # it by suffix rather than hardcoding in case of a future rename.
        member = next((n for n in zf.namelist() if n.endswith("conversations.json")), None)
        if member is None:
            return
        conversations = json.loads(zf.read(member))

    for conv in conversations:
        conv_id = conv.get("uuid")
        if not conv_id:
            continue
        stats["conversations_processed"] += 1

        active_path = _reconstruct_active_path(conv.get("chat_messages") or [])
        # Only messages actually journaled (non-empty text, not excluded by
        # retention) are valid parent references — a message present on the
        # active path but skipped must not be cited as another event's
        # parent_event_ids, or that reference would dangle.
        journaled_ids: set[str] = set()

        for msg in active_path:
            stats["messages_seen"] += 1
            text = _extract_text(msg)
            if not text:
                stats["messages_skipped_empty"] += 1
                continue

            actor_type = SENDER_TO_ACTOR_TYPE.get(msg.get("sender"), "system")
            observed_at = _parse_iso(msg.get("created_at")) or datetime.now(timezone.utc)
            parent_uuid = msg.get("parent_message_uuid")
            parent_event_ids = (
                [f"claude:{conv_id}:{parent_uuid}"] if parent_uuid and parent_uuid in journaled_ids else []
            )

            content = {"text": text, "blocks": msg.get("content") or []}
            event_content = policy.apply(content, content_class="default")
            if event_content is None:
                stats["events_excluded_by_retention"] += 1
                continue

            event = SourceEvent(
                schema_version="1.0",
                event_id=f"claude:{conv_id}:{msg['uuid']}",
                event_type="turn.completed",
                source=SourceProvenance(harness="claude", conversation_id=conv_id, turn_id=msg["uuid"]),
                actor_type=actor_type,
                observed_at=observed_at,
                event_date=observed_at,
                date_precision=DatePrecision.EXACT,
                content=event_content,
                content_hash=compute_content_hash(content),
                parent_event_ids=parent_event_ids,
                metadata={"conversation_name": conv.get("name")},
            )
            inserted = store.append(event)
            stats["events_journaled" if inserted else "events_deduped"] += 1
            journaled_ids.add(msg["uuid"])


def _journal_projects(zip_path: Path, store: SqliteEventStore, policy: RetentionPolicy, stats: dict[str, Any]) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        members = [n for n in zf.namelist() if n.endswith(".json") and "/projects/" in f"/{n}"]
        for member in members:
            project = json.loads(zf.read(member))
            project_id = project.get("uuid")
            updated_at = project.get("updated_at")
            if not project_id or not updated_at:
                continue
            stats["projects_processed"] += 1

            content = {
                "name": project.get("name"),
                "description": project.get("description"),
                "docs": [
                    {"filename": d.get("filename"), "content": d.get("content")}
                    for d in (project.get("docs") or [])
                ],
            }
            event_content = policy.apply(content, content_class="default")
            if event_content is None:
                stats["events_excluded_by_retention"] += 1
                continue

            observed_at = _parse_iso(updated_at) or datetime.now(timezone.utc)
            event = SourceEvent(
                schema_version="1.0",
                event_id=f"claude:project:{project_id}:{updated_at}",
                event_type="project.snapshot",
                source=SourceProvenance(harness="claude"),
                actor_type="user",
                observed_at=observed_at,
                event_date=observed_at,
                date_precision=DatePrecision.EXACT,
                content=event_content,
                content_hash=compute_content_hash(content),
                metadata={"is_starter_project": bool(project.get("is_starter_project"))},
            )
            inserted = store.append(event)
            stats["events_journaled" if inserted else "events_deduped"] += 1


def _reconstruct_active_path(chat_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """See module docstring for the active-leaf heuristic."""
    if not chat_messages:
        return []

    by_uuid = {m["uuid"]: m for m in chat_messages if m.get("uuid")}
    is_parent = {m.get("parent_message_uuid") for m in chat_messages}
    leaves = [m for m in chat_messages if m["uuid"] not in is_parent]
    if not leaves:
        # Cyclic or malformed data; fall back to file order rather than crash.
        return chat_messages

    def _sort_key(m: dict[str, Any]) -> str:
        return m.get("created_at") or ""

    active_leaf = max(leaves, key=_sort_key)

    path: list[dict[str, Any]] = []
    visited: set[str] = set()
    current: Optional[dict[str, Any]] = active_leaf
    while current is not None and current["uuid"] not in visited:
        visited.add(current["uuid"])
        path.append(current)
        parent_uuid = current.get("parent_message_uuid")
        current = by_uuid.get(parent_uuid) if parent_uuid and parent_uuid != ROOT_SENTINEL else None

    path.reverse()
    return path


def _extract_text(msg: dict[str, Any]) -> str:
    text = (msg.get("text") or "").strip()
    if text:
        return text
    # Fall back to concatenating "text"-type content blocks (some messages
    # carry text only inside content blocks, not the top-level `text` field).
    parts = []
    for block in msg.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            parts.append(str(block["text"]))
    return " ".join(parts).strip()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
