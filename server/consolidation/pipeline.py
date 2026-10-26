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
import json
import re
from pathlib import Path
from typing import Any, Optional

from server.consolidation.store import ConsolidationStore
from server.consolidation.threads import ThreadIndex, normalize_key
from server.consolidation.triage import assess_window
from server.consolidation.windowing import Windower, default_windower, group_by_conversation
from server.core.models import SourceEvent
from server.core.rate_limiter import GeminiQuotaExhaustedError
from server.core.tool_errors import is_prompt_too_long
from server.journal.store import SqliteEventStore
from server.providers.context_limit import context_tokens
from server.policies.protocols import (
    ExtractionCategory,
    ExtractionPolicy,
    PolicyContext,
    ReasoningEpisode,
    WindowedExtractionPolicy,
)
from server.adapters.claude_code.project_slug import derive_project_from_path
from server.consolidation.project_aliases import resolve_project
from server.adapters.claude_code.parser import HARNESS_COWORK, TRANSCRIPT_HARNESSES as CLAUDE_TRANSCRIPT_HARNESSES
from server.adapters.claude_cowork.discovery import project_for_journaled_session
from server.proposals import (
    ProposalRefused,
    advance_updated,
    create_doc_proposal,
    list_proposals,
    review_proposal,
    strip_frontmatter,
)
from server.review.actions import reject_episode
from server.review.store import ReviewStore

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


def _retrieve_relevant_wiki_docs(
    window_events: list[SourceEvent],
    project: Optional[str] = None,
    conversation_title: Optional[str] = None,
    wiki_root: Optional[Path] = None,
    max_results: int = 5,
) -> list[dict[str, Any]]:
    """Whole-corpus search for durable knowledge pages relevant to this window.

    Searches across the entire LLM_Wiki corpus (using search_corpus) so that
    ExtractPolicyV1 can ground doc proposals in existing documentation
    (updating an existing page rather than inventing a duplicate).
    """
    try:
        from server.providers.wiki.scanner import search_corpus

        query_parts: list[str] = []
        if project and project not in ("unknown", "other", "tmp-other"):
            query_parts.append(project)
        if conversation_title:
            query_parts.append(conversation_title)

        # Salient user terms from the window
        user_texts = [
            (e.content.get("text") or "")
            for e in window_events
            if e.actor_type == "user"
        ]
        if user_texts:
            query_parts.append(" ".join(user_texts[0].split()[:20]))

        query = " ".join(query_parts).strip()
        if not query:
            return []

        from server.proposals import get_corpus_root

        results = search_corpus(query=query, root_path=wiki_root, max_results=max_results)
        root = wiki_root if wiki_root is not None else get_corpus_root()
        docs: list[dict[str, Any]] = []
        for r in results:
            if not r.relative_path:
                continue
            # The whole live page, not just the matched snippet: an update is a
            # whole-page rewrite, and one written from a 300-char snippet drops
            # most of the page. ExtractPolicy decides what fits in the prompt.
            try:
                current = (Path(root) / r.relative_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                current = None
            docs.append({
                "target_path": r.relative_path,
                "filename": r.filename,
                "title": r.filename.replace(".md", "").replace("-", " "),
                "snippet": (r.matched_snippet or "")[:300],
                "current_content": current,
            })
        return docs
    except Exception:
        logger.debug("Could not retrieve relevant wiki docs for window context", exc_info=True)
        return []


def _context_grew(last_error: Optional[str]) -> bool:
    """True when the model's context is now larger than the one an oversize window failed at."""
    m = re.match(r"context=(\d+):", last_error or "")
    return bool(m) and context_tokens() > int(m.group(1))


def _evaluate_fitting(policy: WindowedExtractionPolicy, events: list[SourceEvent], context: PolicyContext) -> list:
    """policy.evaluate_window, split in half once when the window is too long for the model (B18)."""
    try:
        return policy.evaluate_window(events, context)
    except Exception as exc:  # noqa: BLE001 - only a too-long prompt is handled here
        if not is_prompt_too_long(exc) or len(events) < 2:
            raise
        mid = len(events) // 2
        logger.info("Window of %d events is too long for the model's context; extracting it in two halves.", len(events))
        return policy.evaluate_window(events[:mid], context) + policy.evaluate_window(events[mid:], context)


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
    review_store: Optional[ReviewStore] = None,
    merge_threads: bool = True,
    merge_char_budget: Optional[int] = None,
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
    `triaged_out` without a model call (the user, 2026-09-06 — matters mostly
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

    `merge_threads` (default True, "review by conversation" follow-up,
    2026-09-19): after all of a conversation's windows are processed, tier-1
    episodes sharing a normalized `thread_key` are collapsed into one
    consolidated episode (see `_merge_tier1_by_thread`) so the review queue
    surfaces one item per thread rather than one per window's restatement of
    it. The originals are rejected (reviewer="pipeline-thread-merge") via the
    same `reject_episode` chokepoint a human reviewer uses, not a side door.
    Similarly, a DURABLE_CANDIDATE whose `target_path` repeats one already
    proposed earlier in the same conversation's run supersedes the earlier
    pending proposal instead of coexisting with it as a near-duplicate page.

    `merge_char_budget` (MS4e, default THREAD_MERGE_CHAR_BUDGET) caps how much
    text one merged episode may carry; a longer thread is merged in
    consecutive parts instead. See `_merge_tier1_by_thread`.
    """
    windower = windower or default_windower()
    # Stores this call opens itself are closed on the way out; a caller-passed
    # store stays the caller's. Before this, every call leaked two sqlite
    # connections -- found as 50+ open handles on journal.db during the
    # launchd poller's 872-conversation first run (2026-09-22).
    owned: list[Any] = []
    if thread_index is None:
        thread_index = ThreadIndex(consolidation_store.db_path)
        owned.append(thread_index)
    if review_store is None:
        review_store = ReviewStore(consolidation_store.db_path)
        owned.append(review_store)
    try:
        events = journal_store.query(harness=harness, conversation_id=conversation_id, since=since)
        by_conv = group_by_conversation(events)

        # Best-effort, computed once per run (not per-window/conversation --
        # stable enough, and this is a hint not a hard constraint). Missing/
        # misconfigured wiki root must not fail a run that only needs it for
        # ExtractPolicyV1's doc-proposal routing -- ReasoningEpisodePolicyV1
        # never reads this field, and a run with no doc proposals at all is
        # unaffected either way.
        existing_project_folders: list[str] = []
        existing_wiki_folders: list[str] = []
        corpus_root: Optional[Path] = wiki_root
        try:
            from server.providers.wiki.corpus import get_corpus_root
            corpus_root = wiki_root if wiki_root is not None else get_corpus_root()
            projects_dir = corpus_root / "WIKI" / "projects"
            if projects_dir.is_dir():
                existing_project_folders = sorted(p.name for p in projects_dir.iterdir() if p.is_dir())
            existing_wiki_folders = sorted(
                f"{top}/{p.name}" for top in ("WIKI", "RAW") if (corpus_root / top).is_dir()
                for p in (corpus_root / top).iterdir() if p.is_dir() and not p.name.startswith(".")
            )
        except Exception:  # noqa: BLE001 -- see comment above; this is a hint, not a requirement
            logger.debug("Could not enumerate existing WIKI/projects/ folders for doc-routing context", exc_info=True)

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
            "doc_proposals_skipped": 0,
            "doc_proposals_superseded_in_run": 0,
            "threads_merged": 0,
            "episodes_merged_away": 0,
            "by_reasoning_kind": defaultdict(int),
            "by_approval_state": defaultdict(int),
            "quota_exhausted": False,
            "stopped_at_max_windows": False,
            "windows_oversize": 0,
            "windows_skipped_oversize": 0,
            "errors": [],
        }

        for conv_key in sorted(by_conv, key=lambda k: min(e.observed_at for e in by_conv[k])):
            conv_events = by_conv[conv_key]
            # Reset per-conversation: tier-1 episodes written so far (for the
            # end-of-conversation thread merge, which also picks up this
            # conversation's still-unmerged children from earlier runs). Doc
            # proposals are read back from the proposal store each window.
            tier1_this_conv: list[tuple[str, ReasoningEpisode]] = []
            harness_slug_for_conv: Optional[str] = None
            # The merged episode a thread merge writes needs the project its
            # children carried (found 2026-10-03: merges were written with
            # project NULL, so promotion would have tagged them with nothing).
            project_for_conv: Optional[str] = None

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
                    if existing["status"] == "oversize" and not _context_grew(existing["last_error"]):
                        stats["windows_skipped_oversize"] += 1
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

                harness_slug = window.events[0].source.harness
                harness_slug_for_conv = harness_slug_for_conv or harness_slug
                # Per-harness project derivation (claude_code: found 2026-09-23,
                # MS4b poller review; antigravity: found 2026-09-24, same class
                # of bug on the new adapter). Each harness's transcript metadata
                # names its project differently -- claude_code encodes a path
                # string under project_path that needs derive_project_from_path's
                # heuristic; antigravity's own adapter already resolves a clean
                # slug at extraction time (server.adapters.antigravity.project)
                # and stores it directly under metadata["project"], no heuristic
                # needed here. Stays a contained per-harness branch rather than a
                # generic concept pipeline.py otherwise knows nothing about.
                # window.events are already in memory -- no extra journal lookup.
                # claude_cowork's transcript dir is a sandbox path, so its
                # adapter resolves the project from the session's selected
                # folder into metadata["project"], like antigravity/codex.
                if harness_slug in CLAUDE_TRANSCRIPT_HARNESSES and harness_slug != HARNESS_COWORK:
                    project = derive_project_from_path(window.events[0].metadata.get("project_path"))
                elif harness_slug == HARNESS_COWORK:
                    # Re-derive from the journaled folder/sidecar so the current
                    # folder and scheduled-task maps apply to old events too.
                    project = project_for_journaled_session(window.events[0].metadata)
                elif harness_slug in ("antigravity", "codex"):
                    project = window.events[0].metadata.get("project")
                else:
                    project = None
                project = resolve_project(project)
                project_for_conv = project_for_conv or project

                conv_title = window.events[0].metadata.get("conversation_title")
                relevant_wiki_docs = _retrieve_relevant_wiki_docs(
                    window_events=list(window.events),
                    project=project,
                    conversation_title=conv_title,
                    wiki_root=wiki_root,
                    max_results=5,
                )

                # Pending proposals: this conversation's (any earlier run) and the
                # project's, so new material extends them instead of competing.
                pending_now = [p for p in list_proposals(proposals_dir=proposals_dir, status="pending_review")
                               if p.provider == "wiki"]
                context = PolicyContext(
                    topical_window=list(window.events),
                    open_threads=thread_index.scoped_threads(conversation_id=window.conversation_id or conv_key, project=project, as_of=max(e.observed_at for e in window.events), journal_store=journal_store),
                    open_doc_pages=[p.to_dict() for p in _relevant_pending(pending_now, window.conversation_id, project)],
                    existing_project_folders=existing_project_folders,
                    existing_wiki_folders=existing_wiki_folders,
                    relevant_wiki_docs=relevant_wiki_docs,
                    conversation_title=conv_title,
                )
                consolidation_store.mark_running(job_id, primary, policy.name, policy.version)
                try:
                    episodes = _evaluate_fitting(policy, list(window.events), context)
                except GeminiQuotaExhaustedError:
                    # Leave the job 'running' so it retries next pass; write nothing.
                    stats["quota_exhausted"] = True
                    _finalize(stats)
                    return stats
                except Exception as exc:  # noqa: BLE001 — record any policy failure, keep going
                    if is_prompt_too_long(exc):
                        # Too long even split in half: record once and skip it on
                        # later passes rather than failing every cycle (B18).
                        consolidation_store.record_oversize(job_id, context_tokens(), str(exc))
                        stats["windows_oversize"] += 1
                        logger.warning("Window %s is too long for the model's context even split; skipping it.", win_id)
                        continue
                    consolidation_store.record_failure(job_id, str(exc))
                    stats["windows_failed"] += 1
                    stats["errors"].append(str(exc))
                    continue

                stats["windows_sent_to_model"] += 1
                if not episodes:
                    consolidation_store.mark_succeeded_no_output(job_id)
                    continue
                for idx, episode in enumerate(episodes):
                    if episode.category == ExtractionCategory.DURABLE_CANDIDATE:
                        target_path = episode.target_path or ""
                        try:
                            target_exists = corpus_root is not None and (corpus_root / target_path).is_file()
                            if target_exists and not episode.target_seen:
                                raise ProposalRefused(f"existing page {target_path} was not shown in full; kept as episode material only")
                            # Supersede only an additive (or new-page) proposal; a legacy
                            # whole-page replacement stays for review, flagged as a conflict.
                            prior_here = max(
                                (p for p in pending_now if p.source_conversation_id == window.conversation_id
                                 and p.target_path == target_path and (p.mode == "append" or not target_exists)),
                                key=lambda p: p.created_at, default=None,
                            )
                            material = episode.proposed_content or ""
                            if prior_here is not None:
                                material = _carry_forward(prior_here, material, target_exists, episode.source_date, target_path)
                            proposal = create_doc_proposal(
                                target_path=target_path,
                                proposed_content=material,
                                mode="append",
                                target_heading=episode.target_heading or (prior_here.target_heading if prior_here else None),
                                source_date=episode.source_date,
                                supersedes=prior_here.proposal_id if prior_here else None,
                                rationale=episode.rationale or "",
                                source_context=episode.statement,
                                source_conversation_id=window.conversation_id,
                                source_harness=harness_slug,
                                source_project=project,
                                wiki_root=wiki_root,
                                proposals_dir=proposals_dir,
                            )
                            pending_now.append(proposal)
                            stats["doc_proposals_created"] += 1

                            # Same target_path already proposed earlier THIS RUN
                            # (the model was told about it via open_doc_pages but
                            # invented a fresh page anyway, or ignored the hint) --
                            # supersede the earlier one rather than leave two
                            # pending proposals for the same page competing for
                            # review. The new one already carries the fuller,
                            # more recent content.
                            if prior_here is not None and prior_here.proposal_id != proposal.proposal_id:
                                try:
                                    review_proposal(
                                        prior_here.proposal_id,
                                        verdict="rejected",
                                        reviewer="pipeline-doc-merge",
                                        notes=f"superseded by {proposal.proposal_id}, which carries its material forward",
                                        proposals_dir=proposals_dir,
                                    )
                                    pending_now.remove(prior_here)
                                    stats["doc_proposals_superseded_in_run"] += 1
                                except ValueError as exc:
                                    logger.warning("Could not supersede prior doc proposal %s: %s", prior_here.proposal_id, exc)

                        except ProposalRefused as exc:
                            logger.info("ExtractPolicyV1 doc proposal declined for %s: %s", job_id, exc)
                            stats["doc_proposals_skipped"] += 1
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
                        project=project,
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
                            project=project,
                        )

                    if approval == "queued_for_review" and episode.thread_key:
                        tier1_this_conv.append((memory_id, episode))

            if merge_threads:
                # Include still-unreviewed children from successful earlier windows/reruns.
                present = {mid for mid, _ in tier1_this_conv}
                conn = consolidation_store._conn
                unmerged = ("FROM derived_memories d {join} WHERE d.policy_name=? AND d.policy_version=? "
                            "AND d.approval_state='queued_for_review' AND d.thread_key IS NOT NULL "
                            "AND d.superseded_by IS NULL AND d.memory_id NOT LIKE 'reason:threadmerge:%' {conv}")
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='reviews'").fetchone():
                    # A reviewed child keeps its verdict and never feeds a new merge.
                    unmerged += " AND d.memory_id NOT IN (SELECT memory_id FROM reviews WHERE review_state != 'pending')"
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone():
                    prior_rows = conn.execute(
                        "SELECT d.* " + unmerged.format(join="JOIN events e ON e.event_id = d.source_event_id",
                                                        conv="AND COALESCE(e.conversation_id, e.event_id) = ?"),
                        (policy.name, policy.version, conv_key)).fetchall()
                else:
                    prior_rows = []
                    for row in conn.execute("SELECT d.* " + unmerged.format(join="", conv=""), (policy.name, policy.version)):
                        source = journal_store.get(row["source_event_id"])
                        if source is not None and (source.source.conversation_id or source.event_id) == conv_key:
                            prior_rows.append(row)
                for row in prior_rows:
                    if row["memory_id"] in present:
                        continue
                    tier1_this_conv.append((row["memory_id"], ReasoningEpisode(
                        category=ExtractionCategory.EPISODIC, reasoning_kind=row["reasoning_kind"] or "finding",
                        statement=row["statement"], confidence=row["confidence"],
                        evidence_event_ids=json.loads(row["evidence_event_ids_json"] or "[]"),
                        thread_key=row["thread_key"], event_date=datetime.fromisoformat(row["event_date"]) if row["event_date"] else None,
                    )))
            if merge_threads and len(tier1_this_conv) > 1:
                merge_stats = _merge_tier1_by_thread(
                    consolidation_store=consolidation_store,
                    review_store=review_store,
                    conv_key=conv_key,
                    harness=harness_slug_for_conv or "",
                    project=project_for_conv,
                    policy_name=policy.name,
                    policy_version=policy.version,
                    episodes=tier1_this_conv,
                    char_budget=merge_char_budget,
                )
                stats["threads_merged"] += merge_stats["threads_merged"]
                stats["episodes_merged_away"] += merge_stats["episodes_merged_away"]

        _finalize(stats)
        return stats
    finally:
        for store in owned:
            store.close()


# MS4e: the most text one thread-merged episode may carry, counted as the sum
# of its parts' statement + driving_question + rationale -- the fields
# promotion's enriched_episode_content() sends to Graphiti. Uncapped, one
# 74-turn thread became a 23.5K-char episode that Graphiti turned into 85
# entities (claude-code-jspace-048, 2026-09-28); entity yield grows roughly
# linearly with episode length. ~3K keeps a merge in the size band where
# extraction was well-behaved (1-3K chars: ~8 entities per episode).
THREAD_MERGE_CHAR_BUDGET = 3000


def _episode_text_size(episode: ReasoningEpisode) -> int:
    return sum(len(getattr(episode, f) or "") for f in ("statement", "driving_question", "rationale"))


def _chunk_by_budget(
    group: list[tuple[str, ReasoningEpisode]], char_budget: int
) -> list[list[tuple[str, ReasoningEpisode]]]:
    """Split an ordered thread into consecutive runs whose summed text size
    stays within `char_budget`. An episode already over budget on its own
    gets a run to itself; nothing is dropped or reordered."""
    chunks: list[list[tuple[str, ReasoningEpisode]]] = []
    current: list[tuple[str, ReasoningEpisode]] = []
    size = 0
    for pair in group:
        n = _episode_text_size(pair[1])
        if current and size + n > char_budget:
            chunks.append(current)
            current, size = [], 0
        current.append(pair)
        size += n
    if current:
        chunks.append(current)
    return chunks


def _relevant_pending(pending, conversation_id, project):
    """This conversation's pending proposals first, then the same known project's."""
    own = [p for p in pending if p.source_conversation_id == conversation_id]
    known = project not in (None, "", "unknown", "other", "tmp-other")
    same_project = [p for p in pending if known and p.source_project == project and p not in own]
    return sorted(own, key=lambda p: p.created_at, reverse=True) + sorted(same_project, key=lambda p: p.created_at, reverse=True)


def _carry_forward(prior, material: str, target_exists: bool, source_date, target_path: str) -> str:
    """Combine an unreviewed proposal's material with this window's, losing neither.

    For an existing page the material is the addition; for a new page it is the
    whole page, whose first frontmatter is kept. New material that already
    contains the earlier material (a fuller restatement) replaces it.
    """
    old = (prior.addition if target_exists else prior.proposed_content) or ""
    if not target_exists:
        material = strip_frontmatter(material)
    old_body = old if target_exists else strip_frontmatter(old)
    head = old[: len(old) - len(old_body)] if old.endswith(old_body) else ""
    if not old_body.strip() or material.strip() in old_body:
        combined = old_body if old_body.strip() else material
    elif old_body.strip() in material:
        combined = material
    else:
        combined = old_body.rstrip() + "\n\n" + material.strip() + "\n"
    result = head + combined
    return result if target_exists else advance_updated(result, source_date, target_path)


def _retire_absorbed(consolidation_store, review_store, children, merged_id):
    """Link only unreviewed children demonstrably represented by an existing merge."""
    conn = consolidation_store._conn
    merged = consolidation_store.get_derived_memories([merged_id])
    if not merged:
        return 0
    parent = merged[0]
    evidence = set(json.loads(parent["evidence_event_ids_json"] or "[]"))
    count = 0
    for mid, episode in children:
        row = consolidation_store.get_derived_memories([mid])
        # A child without a derived row is in-memory only; 1.6 retired those too.
        if row and row[0]["approval_state"] != "queued_for_review":
            continue
        review = review_store.get(mid)
        if review is not None and review["review_state"] != "pending":
            if review["reviewer"] == "pipeline-thread-merge" and review["reason"] == f"merged into {merged_id}":
                continue  # retired into this same parent on an earlier run
            logger.warning("Merge conflict: reviewed child %s remains untouched", mid)
            continue
        has_promotions = conn.execute("SELECT 1 FROM sqlite_master WHERE name='promotions'").fetchone()
        if has_promotions and conn.execute("SELECT 1 FROM promotions WHERE memory_id=? AND status='succeeded'", (mid,)).fetchone():
            logger.warning("Merge conflict: promoted child %s remains untouched", mid)
            continue
        if episode.statement not in parent["statement"] or not set(episode.evidence_event_ids).issubset(evidence):
            logger.warning("Merge conflict: %s is not fully represented in %s", mid, merged_id)
            continue
        reject_episode(review_store, mid, reviewer="pipeline-thread-merge", reason=f"merged into {merged_id}")
        conn.execute("UPDATE derived_memories SET superseded_by=? WHERE memory_id=?", (merged_id, mid))
        conn.commit()
        count += 1
    return count


def _merge_tier1_by_thread(
    consolidation_store: ConsolidationStore,
    review_store: ReviewStore,
    conv_key: str,
    harness: str,
    policy_name: str,
    policy_version: str,
    episodes: list[tuple[str, ReasoningEpisode]],
    char_budget: Optional[int] = None,
    project: Optional[str] = None,
) -> dict[str, int]:
    """Collapse this conversation's tier-1 episodes into one per thread.

    "review by conversation" follow-up (2026-09-19): reviewing e2ea026a
    surfaced 3 separate `plan`/`decision` episodes in the
    `cmf-graph-rebuild-monitoring` thread that were really one restated
    plan, and a reviewer wants to approve/reject a thread's worth of
    decisions once, not once per window's restatement. This does NOT
    replace `server.review.actions`' deliberate choice not to build
    `approve_thread`/`reject_thread` (most threads are singletons corpus-
    wide, so a review-time batch action is a solved problem not worth a new
    verb) -- it runs upstream, at extraction time, on a group that is
    already known to be non-trivial (>1 episode), and produces one *episode*
    a reviewer then approves or rejects normally.

    A group of exactly one is left alone (already the common case). A group
    of >1 is merged into a single new episode: statement becomes a numbered
    list of the originals (nothing lost, just consolidated), evidence is the
    ordered union, confidence is the minimum across the group (conservative
    -- a merged claim is only as strong as its weakest part), and
    driving_question/rationale/alternatives are the union of whatever
    distinct non-null values the group carries. `reasoning_kind` picks the
    most review-relevant kind present via a fixed priority order.

    MS4e: a group whose text exceeds `char_budget` (default
    THREAD_MERGE_CHAR_BUDGET) is split into consecutive runs
    (`_chunk_by_budget`) and each run of >1 is merged on its own, with
    `::part<N>` appended to its job and memory ids; a run of one is left as
    the original episode, exactly like a singleton thread. A group within
    budget keeps the unsuffixed ids it always had, and a group already
    merged whole under those ids by an earlier run is not re-split.
    """
    if char_budget is None:
        char_budget = THREAD_MERGE_CHAR_BUDGET
    by_thread: dict[str, list[tuple[str, ReasoningEpisode]]] = defaultdict(list)
    for memory_id, episode in episodes:
        by_thread[normalize_key(episode.thread_key or "")].append((memory_id, episode))

    threads_merged = 0
    episodes_merged_away = 0
    kind_priority = [
        "decision", "plan", "rejected_alternative", "retrospective",
        "finding", "experiment", "hypothesis", "investigation",
    ]

    for norm_key, group in by_thread.items():
        if len(group) < 2:
            continue

        base_job_id = f"job:reason:threadmerge:{conv_key}:{norm_key}::{policy_name}@{policy_version}"
        base_memory_id = f"reason:threadmerge:{conv_key}:{norm_key}::{policy_name}@{policy_version}"
        if consolidation_store.get_job(base_job_id) is not None:
            episodes_merged_away += _retire_absorbed(consolidation_store, review_store, group, base_memory_id)
            continue

        group.sort(key=lambda pair: pair[1].event_date.isoformat() if pair[1].event_date else "")
        chunks = _chunk_by_budget(group, char_budget)
        split = len(chunks) > 1
        merged_any = False

        for part, chunk in enumerate(chunks, start=1):
            if len(chunk) < 2:
                continue
            suffix = f"::part{part}" if split else ""
            merge_job_id = base_job_id + suffix
            if consolidation_store.get_job(merge_job_id) is not None:
                episodes_merged_away += _retire_absorbed(consolidation_store, review_store, chunk, base_memory_id + suffix)
                continue

            kinds_present = {ep.reasoning_kind for _, ep in chunk}
            merged_kind = next((k for k in kind_priority if k in kinds_present), chunk[0][1].reasoning_kind)

            statement = "; ".join(f"({i}) {ep.statement}" for i, (_, ep) in enumerate(chunk, start=1))
            evidence: list[str] = []
            for _, ep in chunk:
                for eid in ep.evidence_event_ids:
                    if eid not in evidence:
                        evidence.append(eid)

            def _union_field(name: str) -> Optional[str]:
                seen, out = set(), []
                for _, ep in chunk:
                    val = getattr(ep, name)
                    if val and val not in seen:
                        seen.add(val)
                        out.append(val)
                return "; ".join(out) if out else None

            merged_episode = ReasoningEpisode(
                category=ExtractionCategory.EPISODIC,
                reasoning_kind=merged_kind,
                statement=statement,
                confidence=min(ep.confidence for _, ep in chunk),
                evidence_event_ids=evidence,
                driving_question=_union_field("driving_question"),
                rationale=_union_field("rationale"),
                alternatives=_union_field("alternatives"),
                status=chunk[-1][1].status,
                thread_key=chunk[0][1].thread_key,
                event_date=chunk[0][1].event_date,
                date_precision=chunk[0][1].date_precision,
            )

            merged_memory_id = base_memory_id + suffix
            consolidation_store.mark_running(merge_job_id, evidence[0] if evidence else "", policy_name, policy_version)
            consolidation_store.record_reasoning_episode(
                job_id=merge_job_id,
                memory_id=merged_memory_id,
                episode=merged_episode,
                policy_name=policy_name,
                policy_version=policy_version,
                approval_state="queued_for_review",
                supersedes=None,
                conversation_id=conv_key,
                harness=harness,
                project=project,
            )

            episodes_merged_away += _retire_absorbed(consolidation_store, review_store, chunk, merged_memory_id)
            merged_any = True

        if merged_any:
            threads_merged += 1

    return {"threads_merged": threads_merged, "episodes_merged_away": episodes_merged_away}


def _finalize(stats: dict[str, Any]) -> None:
    stats["by_reasoning_kind"] = dict(stats["by_reasoning_kind"])
    stats["by_approval_state"] = dict(stats["by_approval_state"])
