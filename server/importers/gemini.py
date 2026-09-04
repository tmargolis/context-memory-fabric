"""Gemini-in-Workspace evidence-emission: journal Conversation History
export turns as SourceEvents.

Scope matches server.importers.chatgpt/claude: evidence only, see
docs/adr/0001-four-layer-model.md.

IMPORTANT SCOPE NOTE: as of the 2026-09-03 Google Takeout export, only
"Gemini in Workspace" (the Docs/Gmail side-panel assistant) conversation
history was available — the main gemini.google.com app's conversation
history was not included in that Takeout request at all (no "Gemini Apps"
product tile was present in the archive manifest; see
IMPLEMENTATION-PLAN.md's Milestone 2 notes). This module only covers what
was actually exported. A future Takeout request selecting the correct
product category would need either an extension to this module or a
sibling one, once that data's real shape is known.

Format: each `conversation_<id>.txt` file is JSON (despite the .txt
extension) shaped as:
    {"conversation_turns": [{"user_turn": {...}} | {"system_turn": {...}}, ...],
     "creation_time": ..., "last_modification_time": ..., "title": ...}
Both turn kinds carry their own `turn_index` and `turn_last_modified`,
unlike ChatGPT/Claude there is no branching structure to reconstruct — the
turns array is already the linear conversation.
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Optional
import json

from server.core.models import DatePrecision, SourceEvent, SourceProvenance
from server.journal.identity import compute_content_hash
from server.journal.retention import RetentionPolicy
from server.journal.store import SqliteEventStore


def journal_gemini_workspace_export(
    conversation_history_dir: Path,
    store: SqliteEventStore,
    retention_policy: Optional[RetentionPolicy] = None,
) -> dict[str, Any]:
    """Journal one SourceEvent per turn across every conversation_*.txt
    file in `conversation_history_dir`.
    """
    policy = retention_policy or RetentionPolicy()
    stats = {
        "files_processed": 0,
        "turns_seen": 0,
        "turns_skipped_empty": 0,
        "events_journaled": 0,
        "events_deduped": 0,
        "events_excluded_by_retention": 0,
    }

    conversation_history_dir = Path(conversation_history_dir)
    for path in sorted(conversation_history_dir.glob("conversation_*.txt")):
        conv_id = path.stem  # e.g. "conversation_1774999569"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        stats["files_processed"] += 1
        title = data.get("title")

        for turn in data.get("conversation_turns") or []:
            stats["turns_seen"] += 1
            if "user_turn" in turn:
                turn_data = turn["user_turn"]
                actor_type = "user"
                text = (turn_data.get("prompt") or "").strip()
            elif "system_turn" in turn:
                turn_data = turn["system_turn"]
                actor_type = "assistant"
                text = " ".join(
                    (block.get("preamble") or "") + (block.get("data") or "")
                    for block in (turn_data.get("text") or [])
                ).strip()
            else:
                stats["turns_skipped_empty"] += 1
                continue

            if not text:
                stats["turns_skipped_empty"] += 1
                continue

            turn_index = turn_data.get("turn_index")
            observed_at = _parse_iso(turn_data.get("turn_last_modified")) or _parse_iso(data.get("creation_time"))
            if observed_at is None:
                stats["turns_skipped_empty"] += 1
                continue

            content = {"text": text, "title": title}
            event_content = policy.apply(content, content_class="default")
            if event_content is None:
                stats["events_excluded_by_retention"] += 1
                continue

            event = SourceEvent(
                schema_version="1.0",
                event_id=f"gemini:{conv_id}:{turn_index}",
                event_type="turn.completed",
                source=SourceProvenance(harness="gemini", conversation_id=conv_id, turn_id=str(turn_index)),
                actor_type=actor_type,
                observed_at=observed_at,
                event_date=observed_at,
                date_precision=DatePrecision.EXACT,
                content=event_content,
                content_hash=compute_content_hash(content),
                metadata={"conversation_title": title, "product": "gemini_in_workspace"},
            )
            inserted = store.append(event)
            stats["events_journaled" if inserted else "events_deduped"] += 1

    return stats


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
