"""Claude evidence-emission: journal conversations, projects, and Claude's
own synthesized memory snapshots as SourceEvents.

Scope matches server.importers.chatgpt: evidence only, no classification —
see that module's docstring and docs/adr/0001-four-layer-model.md.

Accepts either a Claude "Export data" .zip (as originally downloaded) or
an already-extracted plain file/directory for each category — Todd's first
Claude export arrived as four category zips (light_metadata, projects,
memories, conversations), each with a single-use download link; a later
full re-export landed pre-extracted (conversations.json, projects/*.json,
memories/*.json directly). Both forms are genuinely the same data, so this
module reads either without the caller needing to know which.

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

Memory snapshots (conversations_memory / project_memories / memory_files)
are Claude's OWN synthesized summary of the user, not the user's direct
words — actor_type='assistant' is deliberate here, per ROADMAP.md
principle 9: an assistant-authored inference about the user must not, on
its own, establish a personal fact during Milestone 3 consolidation, even
though its subject matter is the user. The export itself carries no
per-snapshot timestamp, so event_id incorporates an externally-supplied
`export_created_at` (the manifest's own created_at) rather than a
timestamp the memory file doesn't have.
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
    conversations_path: Optional[Path] = None,
    projects_path: Optional[Path] = None,
    store: Optional[SqliteEventStore] = None,
    memories_path: Optional[Path] = None,
    export_created_at: Optional[str] = None,
    retention_policy: Optional[RetentionPolicy] = None,
    # Back-compat aliases for the original zip-only signature.
    conversations_zip: Optional[Path] = None,
    projects_zip: Optional[Path] = None,
) -> dict[str, Any]:
    """Journal one SourceEvent per active-path Claude conversation turn,
    one per project snapshot, and one per memory snapshot. Any path may be
    omitted (None) to skip that category. Each path may be a .zip (as
    downloaded) or an already-extracted plain file/directory.
    """
    conversations_path = conversations_path or conversations_zip
    projects_path = projects_path or projects_zip
    if store is None:
        raise TypeError("journal_claude_export() requires store")

    policy = retention_policy or RetentionPolicy()
    stats = {
        "conversations_processed": 0,
        "projects_processed": 0,
        "memory_snapshots_seen": 0,
        "messages_seen": 0,
        "messages_skipped_empty": 0,
        "events_journaled": 0,
        "events_deduped": 0,
        "events_excluded_by_retention": 0,
    }

    if conversations_path is not None:
        _journal_conversations(Path(conversations_path), store, policy, stats)
    if projects_path is not None:
        _journal_projects(Path(projects_path), store, policy, stats)
    if memories_path is not None:
        _journal_memories(Path(memories_path), export_created_at, store, policy, stats)

    return stats


def _load_single_json(path: Path, zip_member_suffix: str) -> Any:
    """Load one JSON document from a .zip member, a plain file, or (for
    the memories case) the first *.json file in a directory.
    """
    if path.is_file() and path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            member = next((n for n in zf.namelist() if n.endswith(zip_member_suffix)), None)
            return json.loads(zf.read(member)) if member else None
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    if path.is_dir():
        candidates = sorted(path.glob("*.json"))
        return json.loads(candidates[0].read_text(encoding="utf-8")) if candidates else None
    return None


def _iter_project_documents(path: Path):
    if path.is_file() and path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            for member in zf.namelist():
                if member.endswith(".json") and "/projects/" in f"/{member}":
                    yield json.loads(zf.read(member))
    elif path.is_dir():
        for p in sorted(path.glob("*.json")):
            yield json.loads(p.read_text(encoding="utf-8"))
    elif path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        yield from data if isinstance(data, list) else [data]


def _journal_conversations(source_path: Path, store: SqliteEventStore, policy: RetentionPolicy, stats: dict[str, Any]) -> None:
    conversations = _load_single_json(source_path, "conversations.json")
    if conversations is None:
        return

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


def _journal_projects(source_path: Path, store: SqliteEventStore, policy: RetentionPolicy, stats: dict[str, Any]) -> None:
    for project in _iter_project_documents(source_path):
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


def _journal_memories(
    source_path: Path,
    export_created_at: Optional[str],
    store: SqliteEventStore,
    policy: RetentionPolicy,
    stats: dict[str, Any],
) -> None:
    data = _load_single_json(source_path, ".json")
    if data is None:
        return

    account_uuid = data.get("account_uuid")
    snapshot_time = export_created_at or datetime.now(timezone.utc).isoformat()
    observed_at = _parse_iso(snapshot_time) or datetime.now(timezone.utc)

    def _emit(event_id: str, text: str, memory_kind: str, extra_metadata: dict[str, Any], conversation_id: Optional[str] = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        stats["memory_snapshots_seen"] += 1
        content = {"text": text, "memory_type": memory_kind}
        event_content = policy.apply(content, content_class="default")
        if event_content is None:
            stats["events_excluded_by_retention"] += 1
            return
        event = SourceEvent(
            schema_version="1.0",
            event_id=event_id,
            event_type="memory_snapshot" if memory_kind != "memory_file" else "memory_file.snapshot",
            source=SourceProvenance(harness="claude", account_scope=account_uuid, conversation_id=conversation_id),
            # Claude's own synthesized summary, not the user's direct words —
            # see module docstring.
            actor_type="assistant",
            observed_at=observed_at,
            event_date=observed_at,
            date_precision=DatePrecision.EXACT,
            content=event_content,
            content_hash=compute_content_hash(content),
            metadata={"memory_kind": memory_kind, **extra_metadata},
        )
        inserted = store.append(event)
        stats["events_journaled" if inserted else "events_deduped"] += 1

    _emit(
        event_id=f"claude:memory:conversations_memory:{account_uuid}:{snapshot_time}",
        text=data.get("conversations_memory", ""),
        memory_kind="conversations_memory",
        extra_metadata={},
    )

    for project_id, memory_text in (data.get("project_memories") or {}).items():
        _emit(
            event_id=f"claude:memory:project:{project_id}:{snapshot_time}",
            text=memory_text,
            memory_kind="project_memory",
            extra_metadata={"project_id": project_id},
            conversation_id=project_id,
        )

    for mf in data.get("memory_files") or []:
        mf_path = mf.get("path")
        if not mf_path:
            continue
        _emit(
            event_id=f"claude:memory_file:{mf_path}:{snapshot_time}",
            text=mf.get("content", ""),
            memory_kind="memory_file",
            extra_metadata={"file_path": mf_path},
        )


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
