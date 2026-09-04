"""Gemini evidence-emission: journal both Google Takeout Gemini product
categories as SourceEvents.

Scope matches server.importers.chatgpt/claude: evidence only, see
docs/adr/0001-four-layer-model.md.

SCOPE HISTORY: the 2026-09-03 Takeout export initially only contained
"Gemini in Workspace" (the Docs/Gmail side-panel assistant) — the main
gemini.google.com app's conversation history was in a separate product
category ("Gemini Apps") that hadn't been selected, and was fetched in a
follow-up export (`journal_gemini_apps_export`, added once that data
existed — see IMPLEMENTATION-PLAN.md's Milestone 2 notes for both rounds).
Both functions are kept as two products are genuinely two different
sources with different real shapes, not one importer with two code paths
for the same thing.

`journal_gemini_workspace_export`: each `conversation_<id>.txt` file is
JSON (despite the .txt extension) shaped as:
    {"conversation_turns": [{"user_turn": {...}} | {"system_turn": {...}}, ...],
     "creation_time": ..., "last_modification_time": ..., "title": ...}
Both turn kinds carry their own `turn_index` and `turn_last_modified`,
unlike ChatGPT/Claude there is no branching structure to reconstruct — the
turns array is already the linear conversation.

`journal_gemini_apps_export`: Google's generic "My Activity" format
(`MyActivity.json`, a flat list — not nested per-conversation like the
Workspace export). Each record is one prompt+response pair (not two
separately-timestamped turns): `title` holds the activity description
(usually, but not always, "Prompted <text>" — other activity types like
"Created a Gem" or "Cleared conversation" appear too and are journaled as
whatever they are, not filtered to only prompt-shaped ones), `time` is the
single timestamp for the pair, `safeHtmlItem[].html` holds the assistant's
response when the activity produced one, and `details[0].url` links back
to the specific gemini.google.com conversation. Emits up to two events per
record (a user event for the title, an assistant event for the response
when present, linked via parent_event_ids) rather than one merged event,
for the same reason server.importers.chatgpt/claude keep user/assistant
content separate: only actor_type='user' content may establish a personal
fact during Milestone 3 consolidation.
"""

from datetime import datetime
from pathlib import Path
import re
from typing import Any, Optional
import html as html_module
import json

from server.core.models import DatePrecision, SourceEvent, SourceProvenance
from server.journal.identity import compute_content_hash, compute_event_id
from server.journal.retention import RetentionPolicy
from server.journal.store import SqliteEventStore

_CONVERSATION_URL_ID_PATTERN = re.compile(r"gemini\.google\.com/app/([a-zA-Z0-9_-]+)")
_TAG_PATTERN = re.compile(r"<[^>]+>")


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


def journal_gemini_apps_export(
    my_activity_json_path: Path,
    store: SqliteEventStore,
    retention_policy: Optional[RetentionPolicy] = None,
) -> dict[str, Any]:
    """Journal Google Takeout's "Gemini Apps" MyActivity.json — the main
    gemini.google.com app's activity/conversation history.
    """
    policy = retention_policy or RetentionPolicy()
    stats = {
        "records_seen": 0,
        "records_skipped_empty": 0,
        "prompt_events_journaled": 0,
        "prompt_events_deduped": 0,
        "response_events_journaled": 0,
        "response_events_deduped": 0,
        "events_excluded_by_retention": 0,
    }

    records = json.loads(Path(my_activity_json_path).read_text(encoding="utf-8"))

    for idx, record in enumerate(records):
        stats["records_seen"] += 1
        title = (record.get("title") or "").strip()
        observed_at = _parse_iso(record.get("time"))
        if not title or observed_at is None:
            stats["records_skipped_empty"] += 1
            continue

        conv_id = _extract_conversation_id(record) or f"unlinked-{idx}"

        prompt_content = {"text": title}
        prompt_event_content = policy.apply(prompt_content, content_class="default")
        prompt_event_id: Optional[str] = None
        if prompt_event_content is None:
            stats["events_excluded_by_retention"] += 1
        else:
            prompt_hash = compute_content_hash(prompt_content)
            prompt_event_id = compute_event_id("gemini", prompt_hash, namespace=f"apps:{conv_id}")
            prompt_event = SourceEvent(
                schema_version="1.0",
                event_id=prompt_event_id,
                event_type="turn.completed",
                source=SourceProvenance(harness="gemini", conversation_id=conv_id),
                actor_type="user",
                observed_at=observed_at,
                event_date=observed_at,
                date_precision=DatePrecision.EXACT,
                content=prompt_event_content,
                content_hash=prompt_hash,
                metadata={"product": "gemini_apps", "activity_url": _first_url(record)},
            )
            inserted = store.append(prompt_event)
            stats["prompt_events_journaled" if inserted else "prompt_events_deduped"] += 1

        response_text, response_html = _extract_response(record)
        if response_text:
            response_content = {"text": response_text, "html": response_html}
            response_event_content = policy.apply(response_content, content_class="default")
            if response_event_content is None:
                stats["events_excluded_by_retention"] += 1
            else:
                response_hash = compute_content_hash(response_content)
                response_event_id = compute_event_id("gemini", response_hash, namespace=f"apps:{conv_id}:response")
                response_event = SourceEvent(
                    schema_version="1.0",
                    event_id=response_event_id,
                    event_type="turn.completed",
                    source=SourceProvenance(harness="gemini", conversation_id=conv_id),
                    actor_type="assistant",
                    observed_at=observed_at,
                    event_date=observed_at,
                    date_precision=DatePrecision.EXACT,
                    content=response_event_content,
                    content_hash=response_hash,
                    parent_event_ids=[prompt_event_id] if prompt_event_id else [],
                    metadata={"product": "gemini_apps", "activity_url": _first_url(record)},
                )
                inserted = store.append(response_event)
                stats["response_events_journaled" if inserted else "response_events_deduped"] += 1

    return stats


def _extract_conversation_id(record: dict[str, Any]) -> Optional[str]:
    for detail in record.get("details") or []:
        url = detail.get("url") or ""
        m = _CONVERSATION_URL_ID_PATTERN.search(url)
        if m:
            return m.group(1)
    return None


def _first_url(record: dict[str, Any]) -> Optional[str]:
    details = record.get("details") or []
    return details[0].get("url") if details else None


def _extract_response(record: dict[str, Any]) -> tuple[str, str]:
    """Return (plain_text, raw_html) for the response, or ("", "") if none.

    Preserves the raw HTML alongside a stripped-tag plain-text form — per
    ROADMAP.md's "evidence is not memory" principle, the stripping here is
    for convenience (a readable `text` field), not a claim that markup-level
    detail was unimportant; the raw form is retained in the same content dict.
    """
    blocks = record.get("safeHtmlItem") or []
    html_parts = [b.get("html", "") for b in blocks if b.get("html")]
    if not html_parts:
        return "", ""
    raw_html = " ".join(html_parts)
    text = html_module.unescape(_TAG_PATTERN.sub(" ", raw_html))
    text = re.sub(r"\s+", " ", text).strip()
    return text, raw_html


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
