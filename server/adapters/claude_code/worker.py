"""MS4b's background worker: tail changed Claude Code / Desktop Code-tab
transcript files, journal new events, then run reasoning-episode
consolidation over the touched conversations.

Intended to run from a launchd agent polling every ~10-15 minutes
(precedent: com.cmf.spark-tunnel.plist), independent of whether Desktop's
Code tab actually fires SessionStart/Stop hooks (unverified as of MS4b —
see docs/plan-active.md). hooks.py's Stop-hook accelerant, if built and
wired, simply calls process_pending() sooner; the poller is the reliable
default either way.

CMF_LLM_PROVIDER is forced to "local" for this worker's own reasoning-
episode extraction, independent of whatever the interactive server /
.env has configured (docs/plan-active.md MS4b: one 8MB/595-turn session
is already ~30 extraction calls, which would blow through Gemini's
free-tier daily quota in a single unattended run with no human review gate
in front of it, unlike promotion). This worker never touches the live
interactive process's os.environ permanently -- it patches, runs, restores.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
import contextlib
import os

from server.adapters.claude_code.parser import ParseStats, parse_line
from server.adapters.claude_code.transcript_reader import (
    TailStateStore,
    discover_transcript_files,
    read_new_lines,
)
from server.consolidation.pipeline import run_reasoning_consolidation
from server.consolidation.store import ConsolidationStore
from server.journal.store import SqliteEventStore
from server.policies.reasoning_episode_v1 import ReasoningEpisodePolicyV1


@dataclass
class WorkerStats:
    files_scanned: int = 0
    files_with_new_bytes: int = 0
    events_journaled: int = 0
    events_deduped: int = 0
    events_skipped_before_cutoff: int = 0
    conversations_touched: set = field(default_factory=set)
    consolidation_runs: list = field(default_factory=list)
    errors: list = field(default_factory=list)


@contextlib.contextmanager
def _forced_local_llm_provider():
    """Force CMF_LLM_PROVIDER=local for the duration of the block, then
    restore whatever was there before -- never leaks into the caller's
    process-wide state.
    """
    previous = os.environ.get("CMF_LLM_PROVIDER")
    os.environ["CMF_LLM_PROVIDER"] = "local"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("CMF_LLM_PROVIDER", None)
        else:
            os.environ["CMF_LLM_PROVIDER"] = previous


def process_pending(
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
    consolidation_store = consolidation_store or ConsolidationStore(journal_store.db_path)
    stats = WorkerStats()

    tail_store = TailStateStore(journal_store.db_path)
    try:
        for transcript in discover_transcript_files(projects_root):
            stats.files_scanned += 1
            session_id = transcript.session_id
            try:
                raw_lines = list(
                    read_new_lines(
                        transcript.path,
                        tail_store,
                        project_slug=transcript.project_slug,
                        session_id=session_id,
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
            stats.conversations_touched.add(session_id)

            for event in events:
                inserted = journal_store.append(event)
                if inserted:
                    stats.events_journaled += 1
                else:
                    stats.events_deduped += 1

        if run_consolidation and stats.conversations_touched:
            with _forced_local_llm_provider():
                policy = ReasoningEpisodePolicyV1()
                for conversation_id in sorted(stats.conversations_touched):
                    try:
                        run_stats = run_reasoning_consolidation(
                            journal_store,
                            consolidation_store,
                            policy,
                            harness="claude_code",
                            conversation_id=conversation_id,
                            reasoning_auto_accept_threshold=reasoning_auto_accept_threshold,
                        )
                        stats.consolidation_runs.append({"conversation_id": conversation_id, **run_stats})
                    except Exception as exc:  # noqa: BLE001 -- isolate per-conversation, never abort the whole poll
                        stats.errors.append(f"consolidation failed for {conversation_id}: {exc}")
    finally:
        tail_store.close()

    return stats


def stats_summary(stats: WorkerStats) -> dict[str, Any]:
    return {
        "files_scanned": stats.files_scanned,
        "files_with_new_bytes": stats.files_with_new_bytes,
        "events_journaled": stats.events_journaled,
        "events_deduped": stats.events_deduped,
        "events_skipped_before_cutoff": stats.events_skipped_before_cutoff,
        "conversations_touched": len(stats.conversations_touched),
        "consolidation_runs": len(stats.consolidation_runs),
        "errors": stats.errors,
    }
