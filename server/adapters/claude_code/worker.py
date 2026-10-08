"""MS4b's background worker: tail changed Claude Code / Desktop Code-tab
transcript files, journal new events, then run ExtractPolicy consolidation
(episodes + doc proposals) over the touched conversations. Switched from
ReasoningEpisodePolicyV1 2026-09-22, for parity with the reviewed
ExtractPolicy path the rest of MS4b was built on -- see docs/plan-active.md.

Intended to run from a launchd agent polling every ~10-15 minutes
(precedent: com.cmf.spark-tunnel.plist), independent of whether Desktop's
Code tab actually fires SessionStart/Stop hooks (unverified as of MS4b —
see docs/plan-active.md). hooks.py's Stop-hook accelerant, if built and
wired, simply calls process_pending() sooner; the poller is the reliable
default either way.

CMF_LLM_PROVIDER is switched to CMF_CAPTURE_LLM_PROVIDER (default "local",
server.adapters.capture_provider) for this worker's own reasoning-episode
extraction, independent of the interactive server's CMF_LLM_PROVIDER
(docs/plan-active.md MS4b: one 8MB/595-turn session is already ~30
extraction calls, which would blow through Gemini's
free-tier daily quota in a single unattended run with no human review gate
in front of it, unlike promotion). This worker never touches the live
interactive process's os.environ permanently -- it patches, runs, restores.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from server.adapters.claude_code.parser import ParseStats, parse_line
from server.adapters.claude_code.transcript_reader import (
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
    conversations_touched: set = field(default_factory=set)
    # conversation_id -> harnesses its new events carried. Since 2026-10-02
    # the parser derives harness from each line's `entrypoint`, so one poll
    # can touch claude_code, claude_desktop_code and claude_cowork
    # conversations; consolidation must query under the real harness or
    # it silently finds no events.
    conversation_harnesses: dict = field(default_factory=dict)
    consolidation_runs: list = field(default_factory=list)
    errors: list = field(default_factory=list)


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
    projects_root: Optional[Path] = None,
    reasoning_auto_accept_threshold: Optional[float] = None,
    run_consolidation: bool = True,
    since: Optional[datetime] = None,
) -> WorkerStats:
    """Tail every allowed project's transcript files for new bytes, journal
    the resulting events, and (if run_consolidation) run reasoning-episode
    consolidation once per touched conversation_id.

    Journaling and consolidation are two separate steps on purpose: a
    consolidation failure (e.g. GeminiQuotaExhaustedError even under the
    forced local provider, or a transient local-inference error) must not
    lose or re-tail already-journaled evidence -- the journal write always
    lands first.

    `since`: drop parsed events with observed_at before this cutoff instead
    of journaling them -- the dedup mitigation from docs/plan-active.md's
    MS4b section (a one-time full-history backfill would otherwise re-walk
    conversations already covered by the "claude"/"gemini"/"chatgpt"
    importers' own cutoffs, under a harness slug their content-hash dedup
    doesn't cross-reference). The tail offset still advances past skipped
    lines either way -- this only controls what gets journaled, not what
    counts as "seen" for the next incremental pass.
    """
    stats = WorkerStats()

    tail_store = TailStateStore(journal_store.db_path)
    owned_store: Optional[ConsolidationStore] = None
    try:
        for transcript in discover_transcript_files(projects_root):
            stats.files_scanned += 1
            session_id = transcript.session_id
            conversation_id = transcript.conversation
            try:
                raw_lines = list(
                    read_new_lines(
                        transcript.path,
                        tail_store,
                        project_slug=transcript.project_slug,
                        session_id=conversation_id,
                    )
                )
            except OSError as exc:
                stats.errors.append(f"{transcript.path}: read failed: {exc}")
                continue

            if not raw_lines:
                continue

            parse_stats = ParseStats()
            events = []
            for raw_line in raw_lines:
                event = parse_line(
                    raw_line,
                    session_id=session_id,
                    project_path=transcript.project_slug,
                    stats=parse_stats,
                    conversation_id=conversation_id,
                    extra_metadata=transcript.extra_metadata,
                )
                if event is None:
                    continue
                if since is not None and event.observed_at < since:
                    stats.events_skipped_before_cutoff += 1
                    continue
                events.append(event)

            if not events:
                continue

            stats.files_with_new_bytes += 1
            stats.conversations_touched.add(conversation_id)
            stats.conversation_harnesses.setdefault(conversation_id, set()).update(e.source.harness for e in events)

            for event in events:
                inserted = journal_store.append(event)
                if inserted:
                    stats.events_journaled += 1
                else:
                    stats.events_deduped += 1

        if run_consolidation and stats.conversations_touched:
            # Constructed lazily, here, not at the top of the function: a
            # caller-passed store is always honored, but the None fallback
            # (real production episode-proposals/, not a sibling of
            # journal_store.db_path -- that was the 2026-09-23 bug, mirrors
            # landing in imports/journal/episode-proposals/ instead of the
            # project root) must never touch the real journal.db/mirror
            # tree for a run_consolidation=False caller (every test in
            # tests/test_ms4b_worker.py relies on that).
            store = consolidation_store or ConsolidationStore(None)
            if consolidation_store is None:
                owned_store = store
            with capture_llm_provider():
                policy = ExtractPolicyV1()
                for conversation_id in sorted(stats.conversations_touched):
                    for harness in sorted(stats.conversation_harnesses.get(conversation_id) or {"claude_code"}):
                        try:
                            run_stats = run_reasoning_consolidation(
                                journal_store,
                                store,
                                policy,
                                harness=harness,
                                conversation_id=conversation_id,
                                reasoning_auto_accept_threshold=reasoning_auto_accept_threshold,
                            )
                            stats.consolidation_runs.append({"conversation_id": conversation_id, "harness": harness, **run_stats})
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
        "conversations_touched": len(stats.conversations_touched),
        "consolidation_runs": len(stats.consolidation_runs),
        "errors": stats.errors,
    }
