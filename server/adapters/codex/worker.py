"""Codex adapter background worker: evidence capture and consolidation (MS4d).

Tails changed Codex transcript files (`rollout-*.jsonl`), journals canonical
source events (`harness="codex"`), and runs ExtractPolicyV1 consolidation
over pending conversations.

Key invariants from docs/CODEX-CAPTURE-PLAN.md:
1. Journal first, then consolidate: Evidence is durably committed to SqliteEventStore
   before extraction runs.
2. Separate pending extraction queue: Pending conversations are tracked in
   `codex_pending_extractions`. If inference fails or quota is exhausted,
   the conversation remains queued and retries on the next poll even if no new
   transcript bytes arrive.
3. Mutual exclusion: WorkerLock guards against overlapping poller and hook invocations.
4. Resource cleanup: Reuses single store instances and closes all owned SQLite
   connections to prevent handle leaks.
5. Local inference safeguard: CMF_LLM_PROVIDER is temporarily switched to
   CMF_CAPTURE_LLM_PROVIDER (default "local") during unattended worker runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging
from pathlib import Path
from typing import Any, Optional

from server.adapters.codex.transcript_reader import (
    CaptureResult,
    TailStateStore,
    TranscriptFile,
    WorkerLock,
    capture_file_to_journal,
    discover_transcript_files,
)
from server.adapters.capture_provider import capture_llm_provider
from server.adapters.spark_lock import spark_slot
from server.consolidation.pipeline import run_reasoning_consolidation
from server.consolidation.store import ConsolidationStore
from server.consolidation.threads import ThreadIndex
from server.journal.store import SqliteEventStore
from server.policies.extract import ExtractPolicyV1
from server.review.store import ReviewStore

logger = logging.getLogger(__name__)


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
    conversations_touched: set[str] = field(default_factory=set)
    extractions_attempted: int = 0
    extractions_succeeded: int = 0
    extractions_failed: int = 0
    consolidation_runs: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    lock_acquired: bool = True


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
    sessions_root: Optional[Path] = None,
    explicit_path: Optional[Path] = None,
    session_id_filter: Optional[str] = None,
    run_consolidation: bool = True,
    reasoning_auto_accept_threshold: Optional[float] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: Optional[int] = None,
    use_lock: bool = True,
    wiki_root: Optional[Path] = None,
    proposals_dir: Optional[Path] = None,
    policy: Optional[Any] = None,
    triage: bool = True,
) -> WorkerStats:
    """Tail changed Codex transcripts, journal events, and consolidate pending conversations.

    If use_lock is True, acquires WorkerLock first; if the lock cannot be obtained
    (e.g. concurrent poller or hook active), returns immediately with lock_acquired=False.
    """
    stats = WorkerStats()

    lock = WorkerLock(journal_store.db_path.parent / "codex_worker.lock") if use_lock else None
    if lock is not None and not lock.acquire():
        logger.info("codex worker: another worker or hook is active, skipping this pass")
        stats.lock_acquired = False
        return stats

    tail_store = TailStateStore(journal_store.db_path)
    owned_consolidation: Optional[ConsolidationStore] = None
    owned_thread_index: Optional[ThreadIndex] = None
    owned_review_store: Optional[ReviewStore] = None

    try:
        # Step 1: Discover and journal evidence from transcripts
        transcripts = discover_transcript_files(
            sessions_root=sessions_root,
            explicit_path=explicit_path,
            session_id_filter=session_id_filter,
            since=since,
            until=until,
            limit=limit,
        )

        for tf in transcripts:
            stats.files_scanned += 1
            cap_res: CaptureResult = capture_file_to_journal(
                tf.path,
                journal_store,
                tail_store,
                since=since,
                until=until,
            )

            stats.lines_read = getattr(stats, "lines_read", 0) + cap_res.lines_read
            stats.events_journaled += cap_res.events_captured
            stats.events_deduped += cap_res.events_deduped
            stats.events_skipped_before_cutoff += cap_res.events_skipped

            if cap_res.files_with_new_bytes > 0:
                stats.files_with_new_bytes += 1
                for cid in cap_res.conversations_touched:
                    stats.conversations_touched.add(cid)
                    # Enqueue extraction work separately from read offsets
                    state = tail_store.get_tail_state(tf.path)
                    tail_store.enqueue_extraction(
                        cid,
                        session_id=state.get("session_id") if state else tf.session_id,
                        project_slug=state.get("project_slug") if state else None,
                    )

            if cap_res.errors:
                stats.errors.extend(cap_res.errors)

        # Step 2: Consolidate pending extractions
        if run_consolidation:
            pending_extractions = tail_store.get_pending_extractions()

            # Filter pending extractions if session_id_filter or explicit_path is active
            if session_id_filter:
                pending_extractions = [
                    pe for pe in pending_extractions if pe["conversation_id"] == session_id_filter
                ]
            elif explicit_path:
                explicit_cids = stats.conversations_touched
                if explicit_cids:
                    pending_extractions = [
                        pe for pe in pending_extractions if pe["conversation_id"] in explicit_cids
                    ]

            if pending_extractions:
                if consolidation_store is None:
                    owned_consolidation = ConsolidationStore(journal_store.db_path)
                    c_store = owned_consolidation
                else:
                    c_store = consolidation_store

                owned_thread_index = ThreadIndex(journal_store.db_path)
                owned_review_store = ReviewStore(journal_store.db_path)

                extraction_policy = policy if policy is not None else ExtractPolicyV1()

                with capture_llm_provider():
                    for pe in pending_extractions:
                        conv_id = pe["conversation_id"]
                        stats.extractions_attempted += 1
                        try:
                            run_stats = run_reasoning_consolidation(
                                journal_store,
                                c_store,
                                extraction_policy,
                                thread_index=owned_thread_index,
                                review_store=owned_review_store,
                                harness="codex",
                                conversation_id=conv_id,
                                reasoning_auto_accept_threshold=reasoning_auto_accept_threshold,
                                triage=triage,
                                wiki_root=wiki_root,
                                proposals_dir=proposals_dir,
                            )
                            stats.consolidation_runs.append({
                                "conversation_id": conv_id,
                                **run_stats,
                            })

                            if run_stats.get("quota_exhausted"):
                                tail_store.record_extraction_failure(conv_id, "Quota exhausted")
                                stats.errors.append(f"Quota exhausted during extraction for {conv_id}")
                                # Stop processing further conversations in this pass
                                break
                            elif run_stats.get("windows_failed", 0) > 0:
                                stats.extractions_failed += 1
                                error_details = ": ".join(run_stats.get("errors", [])) or "unknown error"
                                err_msg = f"Consolidation window failed for {conv_id}: {error_details}"
                                stats.errors.append(err_msg)
                                tail_store.record_extraction_failure(conv_id, err_msg)
                            else:
                                stats.extractions_succeeded += 1
                                tail_store.complete_extraction(conv_id)
                        except Exception as exc:
                            stats.extractions_failed += 1
                            err_msg = f"Consolidation failed for {conv_id}: {exc}"
                            stats.errors.append(err_msg)
                            tail_store.record_extraction_failure(conv_id, str(exc))
                            logger.exception("Codex extraction failed for %s", conv_id)
    finally:
        tail_store.close()
        if owned_consolidation is not None:
            owned_consolidation.close()
        if owned_thread_index is not None:
            owned_thread_index.close()
        if owned_review_store is not None:
            owned_review_store.close()
        if lock is not None:
            lock.release()

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
        "extractions_attempted": stats.extractions_attempted,
        "extractions_succeeded": stats.extractions_succeeded,
        "extractions_failed": stats.extractions_failed,
        "consolidation_runs": len(stats.consolidation_runs),
        "lock_acquired": stats.lock_acquired,
        "errors": stats.errors,
    }
