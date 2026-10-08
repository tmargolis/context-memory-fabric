"""Tail Cowork transcripts, journal them as `claude_cowork`, consolidate the eligible ones.

Same shape as server.adapters.claude_code.worker (same JSONL parser, same
byte-offset tailing), with Cowork's differences:

- harness `claude_cowork` and event_id prefix `claude_cowork:` (no prior
  rows to stay compatible with); offsets in `claude_cowork_tail_state`;
- each event carries the session's sidecar metadata (title, sessionType,
  scheduledTaskId, cloud-handoff id, ...), `project` from the first selected
  folder, and `conversation_title`;
- ExtractPolicy consolidation runs only for interactive / dispatch_child
  sessions; scheduled runs are journal-only unless their scheduledTaskId is
  in CMF_COWORK_EXTRACT_SCHEDULED_TASKS (the user, 2026-10-02);
- `max_conversations` caps how many *extract-eligible* conversations one
  pass takes on. Transcripts past the cap are not read at all, so their
  offsets stay put and the next pass picks them up -- the cap bounds Spark
  load per pass without dropping anything. Journal-only transcripts are
  never capped (no Spark involved);
- the whole pass runs inside server.adapters.spark_lock.spark_slot when it
  will consolidate: if another Spark job holds the slot (or Phase 4 runs),
  the pass is skipped before any tailing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from server.adapters.claude_code.parser import ParseStats, parse_line
from server.adapters.claude_code.transcript_reader import TailStateStore, read_new_lines
from server.adapters.claude_cowork.discovery import discover_transcripts, scheduled_allowlist
from server.adapters.capture_provider import capture_llm_provider
from server.adapters.spark_lock import spark_slot
from server.consolidation.pipeline import run_reasoning_consolidation
from server.consolidation.store import ConsolidationStore
from server.journal.store import SqliteEventStore
from server.policies.extract import ExtractPolicyV1

HARNESS = "claude_cowork"
TAIL_TABLE = "claude_cowork_tail_state"


@dataclass
class WorkerStats:
    skipped_reason: Optional[str] = None
    transcripts_found: int = 0
    files_with_new_bytes: int = 0
    events_journaled: int = 0
    events_deduped: int = 0
    events_skipped_before_cutoff: int = 0
    journal_only_conversations: set = field(default_factory=set)
    extract_conversations: list = field(default_factory=list)
    deferred_by_cap: int = 0
    consolidation_runs: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def _stamp(event, t) -> None:
    # SourceEvent is frozen but its metadata dict is not; parse_line built it.
    md = event.metadata
    md["project"] = t.project
    md["project_folder"] = t.project_folder
    md["cowork_session_id"] = t.cowork_session_id
    md["conversation_title"] = t.sidecar.get("title")
    md["cowork"] = dict(t.sidecar)
    if t.created_at_ms:
        md["cowork_created_at"] = datetime.fromtimestamp(t.created_at_ms / 1000, tz=timezone.utc).isoformat()


def process_pending(
    journal_store: SqliteEventStore,
    consolidation_store: Optional[ConsolidationStore] = None,
    *,
    sessions_root: Optional[Path] = None,
    run_consolidation: bool = True,
    since: Optional[datetime] = None,
    max_conversations: Optional[int] = None,
    reasoning_auto_accept_threshold: Optional[float] = None,
) -> WorkerStats:
    stats = WorkerStats()
    allow = scheduled_allowlist()
    with spark_slot(journal_store.db_path, enabled=run_consolidation) as busy:
        if busy:
            stats.skipped_reason = busy
            return stats
        tail_store = TailStateStore(journal_store.db_path, table=TAIL_TABLE)
        owned_store: Optional[ConsolidationStore] = None
        try:
            transcripts = discover_transcripts(sessions_root)
            stats.transcripts_found = len(transcripts)
            for t in transcripts:
                eligible = run_consolidation and t.extract_eligible(allow)
                if eligible and max_conversations is not None and len(stats.extract_conversations) >= max_conversations:
                    stats.deferred_by_cap += 1
                    continue
                try:
                    raw_lines = list(read_new_lines(t.path, tail_store, project_slug=t.cowork_session_id, session_id=t.session_id))
                except OSError as exc:
                    stats.errors.append(f"{t.path}: read failed: {exc}")
                    continue
                if not raw_lines:
                    continue
                parse_stats = ParseStats()
                events = []
                for raw in raw_lines:
                    event = parse_line(raw, session_id=t.session_id, stats=parse_stats,
                                       default_harness=HARNESS, event_id_prefix=HARNESS)
                    if event is None:
                        continue
                    if since is not None and event.observed_at < since:
                        stats.events_skipped_before_cutoff += 1
                        continue
                    _stamp(event, t)
                    events.append(event)
                if not events:
                    continue
                stats.files_with_new_bytes += 1
                for event in events:
                    if journal_store.append(event):
                        stats.events_journaled += 1
                    else:
                        stats.events_deduped += 1
                if eligible:
                    if t.session_id not in stats.extract_conversations:
                        stats.extract_conversations.append(t.session_id)
                else:
                    stats.journal_only_conversations.add(t.session_id)

            if run_consolidation and stats.extract_conversations:
                store = consolidation_store or ConsolidationStore(None)
                if consolidation_store is None:
                    owned_store = store
                with capture_llm_provider():
                    policy = ExtractPolicyV1()
                    for conversation_id in stats.extract_conversations:
                        try:
                            run_stats = run_reasoning_consolidation(
                                journal_store, store, policy,
                                harness=HARNESS,
                                conversation_id=conversation_id,
                                reasoning_auto_accept_threshold=reasoning_auto_accept_threshold,
                            )
                            stats.consolidation_runs.append({"conversation_id": conversation_id, **run_stats})
                        except Exception as exc:  # noqa: BLE001 -- isolate per conversation
                            stats.errors.append(f"consolidation failed for {conversation_id}: {exc}")
        finally:
            tail_store.close()
            if owned_store is not None:
                owned_store.close()
    return stats


ORDERS = ("yield", "density", "oldest")

# Cowork records much that isn't the user typing as user turns, and some non-
# answers as assistant text (found 2026-10-02: the first density batch picked
# 5 conversations of slash-command expansions and "Unknown skill" retries and
# produced 0 episodes). SQL predicates over `e` for "genuinely typed by the user"
# and "a real assistant reply"; pasted terminal output and images stay the user's.
_TEXT = "json_extract(e.content_json, '$.text')"
_GENUINE_USER = " AND ".join([
    f"substr(ltrim({_TEXT}), 1, 1) != '<'",  # <scheduled-task>, <command-message>, <local-command-caveat>, <uploaded_files>
    f"{_TEXT} NOT LIKE 'Base directory for this skill%'",
    f"{_TEXT} NOT LIKE 'Unknown skill%'",
    f"{_TEXT} NOT LIKE 'Continue from where you left off%'",
    f"{_TEXT} NOT LIKE 'This session is being continued from a previous%'",
])
_REAL_REPLY = " AND ".join([
    f"{_TEXT} NOT LIKE 'No response requested%'",
    f"{_TEXT} NOT LIKE 'You''re out of extra usage%'",
    f"{_TEXT} NOT LIKE 'API Error%'",
])


# A conversation with less assistant prose than this (chars) yielded ~16
# likely-keeper episodes per 1k events vs 47-130 above it (Cowork batches,
# 2026-10-02): too thin to be a discussion worth Spark time early.
THIN_ASSISTANT_CHARS = 2500


@dataclass
class PendingConversation:
    conversation_id: str
    first_event_at: str
    events: int
    typed_turns: int
    real_replies: int
    assistant_chars: int

    @property
    def typed_per_event(self) -> float:
        return self.typed_turns / self.events if self.events else 0.0

    @property
    def prose_per_event(self) -> float:
        return self.assistant_chars / self.events if self.events else 0.0


FEATURES_CACHE_VERSION = 1


def _features_cache_path(journal_store: SqliteEventStore) -> Path:
    return Path(journal_store.db_path).parent / "cowork_features_cache.json"


def load_conversation_features(journal_store: SqliteEventStore, *, use_cache: bool = True) -> list[tuple]:
    """Per-conversation pre-extraction features for every claude_cowork
    conversation: (conversation_id, sessionType, scheduledTaskId, first event,
    events, genuine typed turns, real replies, assistant chars).

    This scans every Cowork event's JSON (~70k rows, hundreds of MB): 2+
    minutes once the journal is large and busy. The features never change
    for journaled events, so they are cached next to the journal and reused
    while the Cowork event count is unchanged (a poller that journals new
    Cowork events invalidates it)."""
    import sqlite3
    db = f"file:{journal_store.db_path}?mode=ro"
    cache = _features_cache_path(journal_store)
    with sqlite3.connect(db, uri=True) as c:
        n_events = c.execute("SELECT count(*) FROM events WHERE harness = ? AND event_id LIKE 'claude_cowork:%'",
                             (HARNESS,)).fetchone()[0]
        if use_cache and cache.exists():
            try:
                data = json.loads(cache.read_text())
                if data.get("version") == FEATURES_CACHE_VERSION and data.get("events") == n_events:
                    return [tuple(r) for r in data["rows"]]
            except (OSError, json.JSONDecodeError, KeyError):
                pass
        rows = c.execute(
            f"""
            SELECT e.conversation_id,
                   max(json_extract(e.metadata_json, '$.cowork.sessionType')),
                   max(json_extract(e.metadata_json, '$.cowork.scheduledTaskId')),
                   min(e.observed_at),
                   count(*),
                   sum(e.actor_type = 'user' AND json_extract(e.content_json, '$.kind') = 'user_turn' AND {_GENUINE_USER}),
                   sum(e.actor_type = 'assistant' AND length(json_extract(e.content_json, '$.text')) > 0 AND {_REAL_REPLY}),
                   sum(CASE WHEN e.actor_type = 'assistant' AND {_REAL_REPLY}
                            THEN length(coalesce(json_extract(e.content_json, '$.text'), '')) ELSE 0 END)
            FROM events e
            WHERE e.harness = ? AND e.event_id LIKE 'claude_cowork:%'
            GROUP BY e.conversation_id
            ORDER BY min(e.observed_at)
            """, (HARNESS,)).fetchall()
    try:
        cache.write_text(json.dumps({"version": FEATURES_CACHE_VERSION, "events": n_events, "rows": rows}))
    except OSError:
        pass
    return rows


def extracted_conversation_ids(journal_store: SqliteEventStore) -> set[str]:
    """Cowork conversations that already have a consolidation job (cheap)."""
    import sqlite3
    with sqlite3.connect(f"file:{journal_store.db_path}?mode=ro", uri=True) as c:
        # A journal no consolidation store has opened yet has no jobs table:
        # then nothing has been extracted.
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='consolidation_jobs'").fetchone():
            return set()
        return {r[0] for r in c.execute(
            "SELECT DISTINCT e.conversation_id FROM consolidation_jobs j JOIN events e ON e.event_id = j.source_event_id "
            "WHERE e.harness = ?", (HARNESS,))}


def pending_extract_ranked(journal_store: SqliteEventStore, order: str = "yield") -> list[PendingConversation]:
    """Pending extract-eligible conversations with their pre-extraction
    features, in `order`. See pending_extract_conversations for the semantics."""
    if order not in ORDERS:
        raise ValueError(f"order must be one of {ORDERS}")
    allow = scheduled_allowlist()
    done = extracted_conversation_ids(journal_store)
    rows = [r for r in load_conversation_features(journal_store) if r[0] not in done]
    eligible: list[PendingConversation] = []
    for conv, stype, task, first, n, typed, replies, a_chars in rows:
        stype = stype or "interactive"
        if stype in ("interactive", "dispatch_child") or (stype == "scheduled" and task in allow):
            eligible.append(PendingConversation(conv, first, n, typed or 0, replies or 0, a_chars or 0))
    floor = _reasoning_floor()
    if order == "yield":
        # the user 2026-10-02, from 40 extracted Cowork conversations: assistant
        # prose per event predicted likely-keeper episodes per event best
        # (r=+0.61; typed-turn density +0.29; tool-call share -0.19). Order:
        # reasonable discussions by prose per event, then thin ones (under
        # THIN_ASSISTANT_CHARS, <2 typed turns or <2 real replies), then
        # stubs below triage's reasoning floor (never reach the LLM).
        eligible.sort(key=lambda r: (
            r.events < floor,
            r.assistant_chars < THIN_ASSISTANT_CHARS or r.typed_turns < 2 or r.real_replies < 2,
            -r.prose_per_event, -r.typed_per_event, r.events))
    elif order == "density":
        # Below triage's reasoning floor (too few events for any window to
        # pass), a conversation never reaches the LLM -- 100% "dense" but
        # zero yield (55 one/two-event stubs, 2026-10-02) -- so it goes last.
        # Next-to-last: fewer than 2 genuinely typed turns or 2 real replies
        # -- plugin-command runs, retries against an errored session, pings.
        eligible.sort(key=lambda r: (r.events < floor, r.typed_turns < 2 or r.real_replies < 2,
                                     -r.typed_per_event, r.events, r.first_event_at))
    else:  # oldest
        eligible.sort(key=lambda r: r.first_event_at)
    return eligible


def pending_extract_conversations(journal_store: SqliteEventStore, order: str = "yield") -> list[str]:
    """Extract-eligible claude_cowork conversations already in the journal
    that no consolidation job has touched. A journal-only backfill (or a
    pass whose consolidation failed) leaves its tail offsets advanced, so
    `tail` would never revisit them; this is how they get extracted afterwards.

    order="yield" (default): assistant prose per event, highest first -- see
    pending_extract_ranked. order="density" (the earlier default): the user's
    typed-turn share first. order="oldest": by first event. Ordering only --
    everything is still extracted."""
    return [r.conversation_id for r in pending_extract_ranked(journal_store, order)]


def _reasoning_floor() -> int:
    import inspect
    from server.consolidation.triage import assess_window
    return inspect.signature(assess_window).parameters["min_events"].default


def triaged_scheduled_conversations(journal_store: SqliteEventStore) -> list[str]:
    """Allowlisted scheduled-task conversations that have windows triage
    withheld ("no user turns in window"). An automated run's only user message
    is the injected task prompt, so triage skips most of its windows -- 61 of
    103 for weekly-market-brief, 3 of 4 for the Bloomberg resume session
    (2026-10-03) -- though the agent's own output is the substance. These are
    re-sent with triage off; windows already extracted are skipped."""
    import sqlite3
    allow = scheduled_allowlist()
    if not allow:
        return []
    marks = ",".join("?" * len(allow))
    with sqlite3.connect(f"file:{journal_store.db_path}?mode=ro", uri=True) as c:
        rows = c.execute(
            f"""SELECT x.conversation_id, min(x.observed_at)
                FROM consolidation_jobs j JOIN events x ON x.event_id = j.source_event_id
                WHERE x.harness = ? AND j.status = 'triaged_out'
                  AND json_extract(x.metadata_json, '$.cowork.sessionType') = 'scheduled'
                  AND json_extract(x.metadata_json, '$.cowork.scheduledTaskId') IN ({marks})
                GROUP BY x.conversation_id ORDER BY min(x.observed_at)""", (HARNESS, *sorted(allow))).fetchall()
    return [r[0] for r in rows]


def extract_pending(
    journal_store: SqliteEventStore,
    consolidation_store: Optional[ConsolidationStore] = None,
    *,
    max_conversations: Optional[int] = None,
    reasoning_auto_accept_threshold: Optional[float] = None,
    order: str = "yield",
    conversations: Optional[list[str]] = None,
    force: bool = False,
    stubs_only: bool = False,
    retriage: bool = False,
) -> WorkerStats:
    """Consolidate up to `max_conversations` pending conversations (see
    pending_extract_conversations) inside the Spark slot. Re-runnable: each
    run continues where the last stopped."""
    stats = WorkerStats()
    with spark_slot(journal_store.db_path) as busy:
        if busy:
            stats.skipped_reason = busy
            return stats
        bypass = stubs_only or retriage
        if retriage:
            pending = triaged_scheduled_conversations(journal_store)
        elif stubs_only:
            # Conversations below triage's reasoning floor (1-2 events: greetings,
            # "usage limit" errors, unknown-skill retries) never reach the model.
            # the user 2026-10-03: run them anyway, "just in case". Only those.
            floor = _reasoning_floor()
            pending = [r.conversation_id for r in pending_extract_ranked(journal_store, order) if r.events < floor]
        else:
            pending = pending_extract_conversations(journal_store, order=order)
        if conversations is not None:  # caller-chosen batch (scripts/cowork_extract_driver.py)
            if force:
                # Finish a conversation an interrupted run left half-done: it
                # already has jobs, so it is no longer "pending", but
                # run_reasoning_consolidation skips windows whose job is done
                # (windows_skipped_done) and only redoes the rest.
                pending = list(dict.fromkeys(conversations))
            else:
                known = set(pending)
                pending = [c for c in conversations if c in known]
        batch = pending[:max_conversations] if max_conversations is not None else pending
        stats.extract_conversations = list(batch)
        stats.deferred_by_cap = len(pending) - len(batch)
        if not batch:
            return stats
        store = consolidation_store or ConsolidationStore(None)
        try:
            with capture_llm_provider():
                policy = ExtractPolicyV1()
                for conversation_id in batch:
                    try:
                        run_stats = run_reasoning_consolidation(
                            journal_store, store, policy, harness=HARNESS, conversation_id=conversation_id,
                            reasoning_auto_accept_threshold=reasoning_auto_accept_threshold,
                            **({"triage": False, "min_window_events": 1} if bypass else {}),
                        )
                        stats.consolidation_runs.append({"conversation_id": conversation_id, **run_stats})
                    except Exception as exc:  # noqa: BLE001 -- isolate per conversation
                        stats.errors.append(f"consolidation failed for {conversation_id}: {exc}")
        finally:
            if consolidation_store is None:
                store.close()
    return stats


def stats_summary(stats: WorkerStats) -> dict[str, Any]:
    return {
        "skipped_reason": stats.skipped_reason,
        "transcripts_found": stats.transcripts_found,
        "files_with_new_bytes": stats.files_with_new_bytes,
        "events_journaled": stats.events_journaled,
        "events_deduped": stats.events_deduped,
        "events_skipped_before_cutoff": stats.events_skipped_before_cutoff,
        "journal_only_conversations": len(stats.journal_only_conversations),
        "extract_conversations": len(stats.extract_conversations),
        "deferred_by_cap": stats.deferred_by_cap,
        "consolidation_runs": len(stats.consolidation_runs),
        "errors": stats.errors,
    }
