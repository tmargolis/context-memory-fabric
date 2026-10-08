"""Antigravity adapter's background worker: tail changed transcript.jsonl
files, journal new events, then run ExtractPolicy consolidation (episodes +
doc proposals) over the touched conversations. Mirrors
server.adapters.claude_code.worker's structure and capture-provider
rationale -- see that module's docstring.

Intended to run from a launchd agent polling every ~15 minutes (precedent:
local.cmf.claude-code-poller.plist), same as claude_code -- that poller, not
any hook, is the reliable backbone; an Antigravity `Stop` hook
(server.adapters.antigravity.hooks), once installed, is only a latency
accelerant that triggers a pass sooner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
import json

from server.adapters.antigravity.parser import ParseStats, parse_line
from server.adapters.antigravity.project import resolve_project
from server.adapters.antigravity.transcript_reader import (
    TailStateStore,
    discover_transcript_files,
    read_new_lines,
)
from server.adapters.capture_provider import capture_llm_provider
from server.adapters.spark_lock import spark_slot
from server.consolidation.pipeline import run_reasoning_consolidation
from server.consolidation.store import ConsolidationStore
from server.journal.store import SqliteEventStore
from server.policies.extract import ExtractPolicyV1


@dataclass
class WorkerStats:
    # Set when the pass was skipped because the Spark slot was busy
    # (server.adapters.spark_lock); nothing was tailed or journaled.
    spark_skipped_reason: Optional[str] = None
    files_scanned: int = 0
    files_with_new_bytes: int = 0
    events_journaled: int = 0
    events_deduped: int = 0
    events_skipped_before_cutoff: int = 0
    events_skipped_after_until: int = 0
    conversations_touched: set = field(default_factory=set)
    consolidation_runs: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def _peek_created_at(raw_line: str) -> Optional[datetime]:
    """Cheap, best-effort peek at a raw transcript line's `created_at`,
    independent of parse_line's keep/skip decision -- the `until` cutoff
    must apply to every line in chronological order, including ones
    parse_line would skip (e.g. a SYSTEM/CHECKPOINT row), not just kept
    ones. Never raises; unparseable/missing timestamps return None and are
    treated as "not past the cutoff" by the caller.
    """
    try:
        record = json.loads(raw_line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    value = record.get("created_at")
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None



def process_pending(journal_store: SqliteEventStore, *args: Any, **kwargs: Any) -> WorkerStats:
    """Run one pass inside the shared Spark slot (server.adapters.spark_lock).

    When the pass will consolidate (run_consolidation, the default) and
    another Spark job holds the slot -- a backfill, another poller, or the
    MS9 Phase 4 wiki extraction -- the pass is skipped before any tailing,
    so no offset advances and the next poll retries. Journal-only passes
    (run_consolidation=False) never touch the lock. Same signature as
    _process_pending_unlocked, which holds the actual pass.
    """
    with spark_slot(journal_store.db_path, enabled=kwargs.get("run_consolidation", True)) as busy:
        if busy:
            stats = WorkerStats()
            stats.spark_skipped_reason = busy
            return stats
        return _process_pending_unlocked(journal_store, *args, **kwargs)

def _process_pending_unlocked(
    journal_store: SqliteEventStore,
    consolidation_store: Optional[ConsolidationStore] = None,
    *,
    app_data_dirs: Optional[list[Path]] = None,
    reasoning_auto_accept_threshold: Optional[float] = None,
    run_consolidation: bool = True,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> WorkerStats:
    """Tail every discovered transcript.jsonl for new bytes, journal the
    resulting events, and (if run_consolidation) run reasoning-episode
    consolidation once per touched conversation_id.

    `since`: drop parsed events with observed_at before this cutoff instead
    of journaling them -- same "don't re-cover ground another importer
    already covered" mitigation as claude_code/worker.py. The tail offset
    still advances past skipped-by-since lines.

    `until`: unlike `since`, this DOES cap how far the tail offset advances
    -- a raw line whose created_at is after `until` stops that file's
    processing for this pass entirely, and the persisted offset lands
    exactly at the end of the last line kept (not end-of-read). This is
    what lets a later `since`-only backfill pick up cleanly from the
    cutoff, e.g. to backfill "everything through last night" without
    disturbing today's still-growing, actively-live conversations. Applied
    to every raw line via _peek_created_at, independent of parse_line's
    keep/skip decision, so a skipped line right at the boundary can't push
    the offset past kept content on the wrong side of the cutoff.
    """
    stats = WorkerStats()

    tail_store = TailStateStore(journal_store.db_path)
    owned_store: Optional[ConsolidationStore] = None
    try:
        for transcript in discover_transcript_files(app_data_dirs):
            stats.files_scanned += 1
            conversation_id = transcript.conversation_id
            app_data_dir_label = transcript.app_data_dir.name

            start_offset = tail_store.get_offset(transcript.path)
            try:
                pairs = list(read_new_lines(transcript.path, tail_store))
            except OSError as exc:
                stats.errors.append(f"{transcript.path}: read failed: {exc}")
                continue

            if not pairs:
                continue

            kept_lines: list[str] = []
            persist_offset = start_offset
            cutoff_hit = False
            for raw_line, offset_after in pairs:
                if until is not None:
                    ts = _peek_created_at(raw_line)
                    if ts is not None and ts > until:
                        cutoff_hit = True
                        break
                kept_lines.append(raw_line)
                persist_offset = offset_after

            if cutoff_hit:
                stats.events_skipped_after_until += len(pairs) - len(kept_lines)

            if not kept_lines:
                # Nothing on this side of the cutoff (or nothing new at
                # all) -- still persist if the offset actually moved, so a
                # run that hits the cutoff on line 1 doesn't re-scan from 0
                # every pass.
                if persist_offset != start_offset:
                    tail_store.set_offset(
                        transcript.path,
                        conversation_id=conversation_id,
                        app_data_dir=app_data_dir_label,
                        byte_offset=persist_offset,
                        line_count_delta=0,
                    )
                continue

            project = resolve_project(conversation_id, transcript.app_data_dir)
            parse_stats = ParseStats()
            events = []
            for raw_line in kept_lines:
                event = parse_line(
                    raw_line,
                    conversation_id=conversation_id,
                    project=project,
                    app_data_dir=app_data_dir_label,
                    stats=parse_stats,
                )
                if event is None:
                    continue
                if since is not None and event.observed_at < since:
                    stats.events_skipped_before_cutoff += 1
                    continue
                events.append(event)

            tail_store.set_offset(
                transcript.path,
                conversation_id=conversation_id,
                app_data_dir=app_data_dir_label,
                byte_offset=persist_offset,
                line_count_delta=len(kept_lines),
            )

            if not events:
                continue

            stats.files_with_new_bytes += 1
            stats.conversations_touched.add(conversation_id)

            for event in events:
                inserted = journal_store.append(event)
                if inserted:
                    stats.events_journaled += 1
                else:
                    stats.events_deduped += 1

        if run_consolidation and stats.conversations_touched:
            store = consolidation_store or ConsolidationStore(None)
            if consolidation_store is None:
                owned_store = store
            with capture_llm_provider():
                policy = ExtractPolicyV1()
                for conversation_id in sorted(stats.conversations_touched):
                    try:
                        run_stats = run_reasoning_consolidation(
                            journal_store,
                            store,
                            policy,
                            harness="antigravity",
                            conversation_id=conversation_id,
                            reasoning_auto_accept_threshold=reasoning_auto_accept_threshold,
                        )
                        stats.consolidation_runs.append({"conversation_id": conversation_id, **run_stats})
                    except Exception as exc:  # noqa: BLE001 -- isolate per-conversation, never abort the whole poll
                        stats.errors.append(f"consolidation failed for {conversation_id}: {exc}")
    finally:
        tail_store.close()
        if owned_store is not None:
            owned_store.close()

    return stats


def stats_summary(stats: WorkerStats) -> dict[str, Any]:
    return {
        "spark_skipped_reason": stats.spark_skipped_reason,
        "files_scanned": stats.files_scanned,
        "files_with_new_bytes": stats.files_with_new_bytes,
        "events_journaled": stats.events_journaled,
        "events_deduped": stats.events_deduped,
        "events_skipped_before_cutoff": stats.events_skipped_before_cutoff,
        "events_skipped_after_until": stats.events_skipped_after_until,
        "conversations_touched": len(stats.conversations_touched),
        "consolidation_runs": len(stats.consolidation_runs),
        "errors": stats.errors,
    }
