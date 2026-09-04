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
from typing import Any, Optional

from server.consolidation.store import ConsolidationStore
from server.core.models import SourceEvent
from server.journal.store import SqliteEventStore
from server.policies.protocols import ExtractionCategory, ExtractionPolicy, PolicyContext

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
