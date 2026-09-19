"""Consolidation pipeline: capture -> normalize -> redact -> classify ->
extract -> reconcile -> approve -> write.

Reads already-journaled SourceEvents (capture already happened, in
Milestone 2's importers) and turns them into derived_memories rows via an
ExtractionPolicy. Stage mapping:

    capture    -> journal_store.query(...) (nothing to do here; the event
                  already exists)
    normalize  -> trivial today (event.content is already the importer's
                  normalized form); a real second normalization pass has
                  no work to do until a second, differently-shaped importer
                  needs reconciling against this one's conventions
    redact     -> server.journal.retention.RetentionPolicy, applied at
                  capture time (Milestone 2) — reapplying it here would be
                  redundant since the journal never stores excluded/
                  unredacted content in the first place
    classify   -> policy.evaluate()
    extract    -> policy.evaluate()'s ExtractionResult IS the extracted
                  candidate; there is no separate step because
                  HeuristicPatternPolicyV1 classifies and extracts in one
                  pass (see its docstring)
    reconcile  -> latest_derivation_for_event() finds a prior derivation
                  under a different policy version to link via `supersedes`
    approve    -> _approval_state_for(): auto_accepted / queued_for_review
                  / rejected, per the Milestone 3 exit gate
    write      -> ConsolidationStore.record_consolidation() — to the local
                  consolidation store, NOT to Graphiti (see module-level
                  note in IMPLEMENTATION-PLAN.md's Milestone 3 section:
                  deriving new episodic memories from the full journal is
                  explicitly out of scope until the Milestone 4a
                  privacy/cost gate is answered)

This module never ingests into Graphiti and never spends an extraction API
call — HeuristicPatternPolicyV1 is pure local pattern matching, not a
model call, so running this pipeline is free (see the Milestone 3 exit
gate discussion of why that matters for auto-accept policy).
"""

from collections import defaultdict
from datetime import datetime
import logging
from pathlib import Path
from typing import Any, Optional

from server.consolidation.store import ConsolidationStore
from server.consolidation.threads import ThreadIndex
from server.consolidation.triage import assess_window
from server.consolidation.windowing import Windower, default_windower, group_by_conversation
from server.core.models import SourceEvent
from server.core.rate_limiter import GeminiQuotaExhaustedError
from server.journal.store import SqliteEventStore
from server.policies.protocols import (
    ExtractionCategory,
    ExtractionPolicy,
    PolicyContext,
    ReasoningEpisode,
    WindowedExtractionPolicy,
)
from server.proposals import create_doc_proposal

logger = logging.getLogger(__name__)

# Milestone 3 exit gate: auto-accept only user-stated, explicitly-dated
# episodic candidates above this confidence. Set from the labeled fixture
# set (tests/fixtures/memory_quality/) — see IMPLEMENTATION-PLAN.md's
# Milestone 3 exit gate for the precision/recall numbers behind this
# specific value.
DEFAULT_AUTO_ACCEPT_THRESHOLD = 0.75


def run_consolidation(
    journal_store: SqliteEventStore,
    consolidation_store: ConsolidationStore,
    policy: ExtractionPolicy,
    harness: Optional[str] = None,
    conversation_id: Optional[str] = None,
    since: Optional[datetime] = None,
    auto_accept_threshold: float = DEFAULT_AUTO_ACCEPT_THRESHOLD,
) -> dict[str, Any]:
    """Run `policy` over matching journal events, writing derived_memories.

    Idempotent: an event already successfully processed under this exact
    (policy.name, policy.version) is skipped, not reprocessed. Running
    again with a policy carrying a *different* version reprocesses every
    matching event and links each new derivation to its predecessor via
    `supersedes`, per Milestone 3's "reprocessing creates a new derivation
    version rather than overwriting lineage."
    """
    events = journal_store.query(harness=harness, conversation_id=conversation_id, since=since)

    stats: dict[str, Any] = {
        "events_seen": 0,
        "already_processed_skipped": 0,
        "events_failed": 0,
        "by_category": defaultdict(int),
        "by_approval_state": defaultdict(int),
        "re_derivations": 0,
    }

    by_conversation: dict[str, list[SourceEvent]] = defaultdict(list)
    for event in events:
        key = event.source.conversation_id or event.event_id
        by_conversation[key].append(event)

    for conv_events in by_conversation.values():
        conv_events.sort(key=lambda e: e.observed_at)
        preceding_assistant_text: Optional[str] = None

        for event in conv_events:
            stats["events_seen"] += 1
            context = PolicyContext(
                preceding_assistant_text=preceding_assistant_text,
                conversation_title=event.metadata.get("conversation_title") or event.metadata.get("conversation_name"),
                section_heading=event.content.get("section_heading"),
            )

            result_stats = _consolidate_one(consolidation_store, policy, event, context, auto_accept_threshold)
            for key_name, value in result_stats.items():
                if key_name in ("already_processed_skipped", "events_failed", "re_derivations"):
                    stats[key_name] += value
                elif key_name == "category":
                    stats["by_category"][value] += 1
                elif key_name == "approval_state":
                    stats["by_approval_state"][value] += 1

            if event.actor_type == "assistant":
                preceding_assistant_text = event.content.get("text")

    stats["by_category"] = dict(stats["by_category"])
    stats["by_approval_state"] = dict(stats["by_approval_state"])
    return stats


def _consolidate_one(
    store: ConsolidationStore,
    policy: ExtractionPolicy,
    event: SourceEvent,
    context: PolicyContext,
    auto_accept_threshold: float,
) -> dict[str, Any]:
    memory_id = f"{event.event_id}::{policy.name}@{policy.version}"
    job_id = f"job:{memory_id}"

    existing_job = store.get_job(job_id)
    if existing_job is not None and existing_job["status"] == "succeeded":
        return {"already_processed_skipped": 1}

    # A job found 'running' here means a prior run crashed before reaching
    # record_consolidation() — see store.py's module docstring. Retried,
    # not skipped.
    store.mark_running(job_id, event.event_id, policy.name, policy.version)

    try:
        result = policy.evaluate(event, context)
    except Exception as exc:  # noqa: BLE001 - genuinely want to catch and record any policy failure
        store.record_failure(job_id, str(exc))
        return {"events_failed": 1}

    prior = store.latest_derivation_for_event(event.event_id, exclude_memory_id=memory_id)
    approval_state = _approval_state_for(result, auto_accept_threshold)

    store.record_consolidation(
        job_id=job_id,
        memory_id=memory_id,
        source_event_id=event.event_id,
        policy_name=policy.name,
        policy_version=policy.version,
        result=result,
        approval_state=approval_state,
        supersedes=prior["memory_id"] if prior is not None else None,
    )

    out: dict[str, Any] = {"category": result.category.value, "approval_state": approval_state}
    if prior is not None:
        out["re_derivations"] = 1
    return out


def _approval_state_for(result, auto_accept_threshold: float) -> str:
    """Milestone 3 exit gate policy: auto-accept only user-stated,
    explicitly-dated episodic candidates above the confidence threshold;
    queue everything else for review. NON_MEMORY (including every
    assistant-authored event, per the policy-level actor-type guard) is
    rejected outright — see HeuristicPatternPolicyV1's module docstring.
    """
    if result.category == ExtractionCategory.NON_MEMORY:
        return "rejected"
    if result.category == ExtractionCategory.EPISODIC and result.confidence >= auto_accept_threshold:
        return "auto_accepted"
    return "queued_for_review"


# --------------------------------------------------------------------------
# MS3.5 — the windowed reasoning stage (ADR 0005). Runs ALONGSIDE
# run_consolidation, not instead of it: an event can get a v1 per-event
# lexical derivation and also be part of a windowed reasoning episode. This
# stage DOES spend model calls (rate-limited Gemini) — one per window that
# clears triage — so it is bounded by the rate limiter and, for probing, by
# `max_windows`.
# --------------------------------------------------------------------------
def _reasoning_approval_state(episode: ReasoningEpisode, threshold: Optional[float]) -> str:
    """ADR 0005 decision 5: reasoning episodes may auto-accept on the
    model's own confidence, without the per-event path's dated-decision
    requirement. `threshold=None` means "never auto-accept yet" — the
    prototype default until the number is re-derived from the rebuilt
    memory-quality fixture (MS3.5 Phase D / exit gate).
    """
    if threshold is None:
        return "queued_for_review"
    return "auto_accepted" if episode.confidence >= threshold else "queued_for_review"


def _window_id(conv_key: str, window_events: list[SourceEvent]) -> str:
    return f"{conv_key}:{window_events[0].event_id}:{window_events[-1].event_id}"


def run_reasoning_consolidation(
    journal_store: SqliteEventStore,
    consolidation_store: ConsolidationStore,
    policy: WindowedExtractionPolicy,
    *,
    thread_index: Optional[ThreadIndex] = None,
    windower: Optional[Windower] = None,
    harness: Optional[str] = None,
    conversation_id: Optional[str] = None,
    since: Optional[datetime] = None,
    triage: bool = True,
    min_window_events: int = 3,
    reasoning_auto_accept_threshold: Optional[float] = None,
    max_windows: Optional[int] = None,
    wiki_root: Optional[Path] = None,
    proposals_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Segment matching journal events into topical windows and derive
    reasoning episodes (and, for a policy that also emits them --
    ExtractPolicyV1 -- doc proposals) for each window that clears triage.

    Idempotent per (window, policy@version): a window whose job already
    succeeded (or was triaged out, while `triage` is on) is skipped.
    Reprocessing under a bumped `policy.version` re-derives and links via
    `supersedes`, same guarantee as run_consolidation.

    `min_window_events` (default 3) is a size floor applied in triage: a
    window below it is a single one-shot exchange and is withheld as
    `triaged_out` without a model call (Todd, 2026-09-06 — matters mostly
    for the Gemini slice). Pass `min_window_events=1` to disable.

    Stops cleanly on `GeminiQuotaExhaustedError` — the offending window's
    job is left retryable, nothing partial is written, and the evidence is
    already safe in the journal.

    `wiki_root`/`proposals_dir` are test-only (default None -> real paths,
    same convention server.capture.session_capture.capture_session already
    follows) -- passed through untouched to create_doc_proposal() for any
    DURABLE_CANDIDATE item a policy emits (see ReasoningEpisode's docstring
    in server.policies.protocols for the two shapes evaluate_window() can
    return). ReasoningEpisodePolicyV1 never emits that category, so these
    params are inert no-ops for it.
    """
    windower = windower or default_windower()
    thread_index = thread_index or ThreadIndex(consolidation_store.db_path)

    events = journal_store.query(harness=harness, conversation_id=conversation_id, since=since)
    by_conv = group_by_conversation(events)

    stats: dict[str, Any] = {
        "conversations_seen": len(by_conv),
        "windows_seen": 0,
        "windows_skipped_done": 0,
        "windows_triaged_out": 0,
        "windows_sent_to_model": 0,
        "windows_failed": 0,
        "episodes_created": 0,
        "doc_proposals_created": 0,
        "doc_proposals_failed": 0,
        "by_reasoning_kind": defaultdict(int),
        "by_approval_state": defaultdict(int),
        "quota_exhausted": False,
        "stopped_at_max_windows": False,
    }

    for conv_key in sorted(by_conv):
        conv_events = by_conv[conv_key]
        for window in windower.windows(conv_events):
            if not window.events:
                continue
            stats["windows_seen"] += 1
            win_id = _window_id(conv_key, window.events)
            job_id = f"job:reason:{win_id}::{policy.name}@{policy.version}"
            primary = next((e.event_id for e in window.events if e.actor_type == "user"), window.events[0].event_id)

            existing = consolidation_store.get_job(job_id)
            if existing is not None:
                if existing["status"] == "succeeded":
                    stats["windows_skipped_done"] += 1
                    continue
                if existing["status"] == "triaged_out" and triage:
                    stats["windows_skipped_done"] += 1
                    continue

            if triage:
                verdict = assess_window(window, min_events=min_window_events)
                if not verdict.send:
                    consolidation_store.record_triaged_out(job_id, primary, policy.name, policy.version, verdict.reason)
                    stats["windows_triaged_out"] += 1
                    continue

            if max_windows is not None and stats["windows_sent_to_model"] >= max_windows:
                stats["stopped_at_max_windows"] = True
                _finalize(stats)
                return stats

            context = PolicyContext(
                topical_window=list(window.events),
                open_threads=thread_index.open_threads(),
                conversation_title=window.events[0].metadata.get("conversation_title"),
            )
            consolidation_store.mark_running(job_id, primary, policy.name, policy.version)
            try:
                episodes = policy.evaluate_window(list(window.events), context)
            except GeminiQuotaExhaustedError:
                # Leave the job 'running' so it retries next pass; write nothing.
                stats["quota_exhausted"] = True
                _finalize(stats)
                return stats
            except Exception as exc:  # noqa: BLE001 — record any policy failure, keep going
                consolidation_store.record_failure(job_id, str(exc))
                stats["windows_failed"] += 1
                continue

            stats["windows_sent_to_model"] += 1
            if not episodes:
                consolidation_store.mark_succeeded_no_output(job_id)
                continue

            harness_slug = window.events[0].source.harness
            for idx, episode in enumerate(episodes):
                if episode.category == ExtractionCategory.DURABLE_CANDIDATE:
                    try:
                        create_doc_proposal(
                            target_path=episode.target_path or "",
                            proposed_content=episode.proposed_content or "",
                            rationale=episode.rationale or "",
                            source_context=episode.statement,
                            source_conversation_id=window.conversation_id,
                            source_harness=harness_slug,
                            wiki_root=wiki_root,
                            proposals_dir=proposals_dir,
                        )
                        stats["doc_proposals_created"] += 1
                    except (ValueError, RuntimeError) as exc:
                        # A malformed target_path or an unconfigured knowledge
                        # provider must not lose the window's episodes or abort
                        # the run -- same one-item-doesn't-sink-the-batch
                        # posture as _capture_doc_item in session_capture.py.
                        logger.warning("ExtractPolicyV1 doc proposal dropped for %s: %s", job_id, exc)
                        stats["doc_proposals_failed"] += 1
                    continue

                memory_id = f"reason:{win_id}:{idx}::{policy.name}@{policy.version}"
                anchor = episode.evidence_event_ids[0] if episode.evidence_event_ids else primary
                prior = consolidation_store.latest_derivation_for_event(anchor, exclude_memory_id=memory_id)
                approval = _reasoning_approval_state(episode, reasoning_auto_accept_threshold)
                consolidation_store.record_reasoning_episode(
                    job_id=job_id,
                    memory_id=memory_id,
                    episode=episode,
                    policy_name=policy.name,
                    policy_version=policy.version,
                    approval_state=approval,
                    supersedes=prior["memory_id"] if prior is not None else None,
                    conversation_id=window.conversation_id,
                    harness=harness_slug,
                )
                stats["episodes_created"] += 1
                stats["by_reasoning_kind"][episode.reasoning_kind] += 1
                stats["by_approval_state"][approval] += 1

                if episode.thread_key:
                    thread_index.record_episode(
                        thread_key=episode.thread_key,
                        title=episode.thread_key,
                        reasoning_kind=episode.reasoning_kind,
                        event_ids=episode.evidence_event_ids,
                        harness=harness_slug,
                        conversation_id=window.conversation_id,
                        observed_at=episode.event_date,
                        status=episode.status,
                    )

    _finalize(stats)
    return stats


def _finalize(stats: dict[str, Any]) -> None:
    stats["by_reasoning_kind"] = dict(stats["by_reasoning_kind"])
    stats["by_approval_state"] = dict(stats["by_approval_state"])
