"""capture_session (MS4a2): live episode/doc-proposal capture from Cowork.

Cowork keeps no local transcript and exposes no hook API (docs/ROADMAP.md's
Milestone 4 section) -- the model itself, mid-conversation, is the only
thing with access to a full session. This module lets the live model apply
the same extraction rubric ReasoningEpisodePolicyV1 applies offline to a
saved transcript, and stage the result through the identical review path,
without ever bypassing MS3.5's "no auto-accept without a human-set
threshold" default.

Two destinations per item:

- "episode": journals the model's own quoted/paraphrased evidence_text as a
  lightweight source event first (Cowork's raw turns are never otherwise
  journaled -- this is the only trace that reaches the journal), then
  stages a ReasoningEpisode via the same ConsolidationStore.record_
  reasoning_episode() the offline pipeline uses, under a distinct
  policy_name ("cowork_live_v1") so provenance stays honest about which
  path produced it.
- "doc_proposal": no new plumbing -- calls the existing
  create_doc_proposal() unchanged.

approval_state is read from CMF_REASONING_AUTO_ACCEPT_THRESHOLD, the same
env var MS4b's (not yet built) offline worker is designed to share -- one
calibration, not two. Unset (the default) means "queued_for_review" for
every episode item, matching MS3.5's own default.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
from typing import Any, Optional
import uuid as uuidlib

from server.capture import filters, identity
from server.consolidation.store import ConsolidationStore
from server.core.models import DatePrecision, REASONING_KINDS, SourceEvent, SourceProvenance
from server.journal.identity import compute_content_hash, compute_event_id
from server.journal.store import SqliteEventStore
from server.policies.protocols import ExtractionCategory, ReasoningEpisode
from server.proposals import create_doc_proposal

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"
EVENT_TYPE_SESSION_EVIDENCE = "capture_session_evidence"
POLICY_NAME = "cowork_live_v1"
POLICY_VERSION = "0.1"

VALID_DESTINATIONS = ("episode", "doc_proposal")


@dataclass
class SessionItemResult:
    destination: str
    ok: bool
    detail: str
    memory_id: Optional[str] = None
    proposal_id: Optional[str] = None
    error: Optional[str] = None


def _reasoning_auto_accept_threshold() -> Optional[float]:
    """Shared with MS4b's (not yet built) offline worker -- one env var,
    one calibration. Unset means "never auto-accept", MS3.5's own default."""
    raw = os.getenv("CMF_REASONING_AUTO_ACCEPT_THRESHOLD")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("CMF_REASONING_AUTO_ACCEPT_THRESHOLD=%r is not a float; treating as unset", raw)
        return None


def _approval_state(confidence: float, threshold: Optional[float]) -> str:
    if threshold is None:
        return "queued_for_review"
    return "auto_accepted" if confidence >= threshold else "queued_for_review"


def _client_info_for(session: Any) -> Optional[Any]:
    client_params = getattr(session, "client_params", None)
    return getattr(client_params, "client_info", None) if client_params is not None else None


def _journal_evidence(
    evidence_text: str, project: Optional[str], session: Any, request_id: Any, db_path: Optional[Any] = None
) -> SourceEvent:
    """Journal one item's evidence_text as a lightweight, synchronous
    source event -- deliberately NOT the fire-and-forget queue path
    tool-call capture uses, because record_reasoning_episode() needs a
    real, already-persisted event_id to cite before it can stage the
    episode; there is no later point to reconcile against.
    """
    client_info = _client_info_for(session)
    harness = identity.resolve_harness(client_info)
    session_id = identity.resolve_session_id(session)

    redacted, redacted_count = filters.redact_secrets_and_count({"evidence": evidence_text})

    payload = {"evidence": redacted["evidence"], "project": project}
    content_hash = compute_content_hash(payload)
    event_id = compute_event_id(
        harness=harness,
        content_hash=content_hash,
        conversation_id=session_id,
        turn_id=f"{request_id}:{uuidlib.uuid4().hex[:8]}" if request_id is not None else None,
    )

    event = SourceEvent(
        schema_version=SCHEMA_VERSION,
        event_id=event_id,
        event_type=EVENT_TYPE_SESSION_EVIDENCE,
        source=SourceProvenance(
            harness=harness,
            conversation_id=session_id,
            session_id=session_id,
            turn_id=str(request_id) if request_id is not None else None,
        ),
        observed_at=datetime.now(timezone.utc),
        content=payload,
        content_hash=content_hash,
        actor_type="user",
        date_precision=DatePrecision.NONE,
        metadata={"client_version": identity.client_version(client_info), "redacted_field_count": redacted_count},
    )

    journal_store = SqliteEventStore(db_path=db_path)
    try:
        journal_store.append(event)
    finally:
        journal_store.close()
    return event


def _capture_episode_item(
    item: dict[str, Any],
    project: Optional[str],
    source_description: Optional[str],
    session: Any,
    request_id: Any,
    consolidation_store: ConsolidationStore,
    db_path: Optional[Any] = None,
) -> SessionItemResult:
    statement = (item.get("statement") or "").strip()
    if not statement:
        return SessionItemResult("episode", False, "", error="'statement' is required for destination='episode'.")

    reasoning_kind = (item.get("reasoning_kind") or "").strip()
    if reasoning_kind not in REASONING_KINDS:
        return SessionItemResult(
            "episode", False, "",
            error=f"'reasoning_kind' must be one of {sorted(REASONING_KINDS)}, got {reasoning_kind!r}.",
        )

    try:
        confidence = float(item.get("confidence"))
    except (TypeError, ValueError):
        return SessionItemResult("episode", False, "", error="'confidence' must be a number between 0 and 1.")
    if not (0.0 <= confidence <= 1.0):
        return SessionItemResult("episode", False, "", error="'confidence' must be between 0 and 1.")

    evidence_text = (item.get("evidence_text") or "").strip()
    if not evidence_text:
        return SessionItemResult("episode", False, "", error="'evidence_text' is required for every item.")

    event = _journal_evidence(evidence_text, project, session, request_id, db_path=db_path)

    episode = ReasoningEpisode(
        category=ExtractionCategory.EPISODIC,
        reasoning_kind=reasoning_kind,
        statement=statement,
        confidence=confidence,
        evidence_event_ids=[event.event_id],
        driving_question=item.get("driving_question") or None,
        rationale=item.get("rationale") or source_description or None,
        thread_key=item.get("thread_key") or None,
    )

    job_id = f"job:cowork_live:{event.event_id}::{POLICY_NAME}@{POLICY_VERSION}"
    memory_id = f"cowork_live:{event.event_id}::{POLICY_NAME}@{POLICY_VERSION}"
    approval_state = _approval_state(confidence, _reasoning_auto_accept_threshold())

    consolidation_store.mark_running(job_id, event.event_id, POLICY_NAME, POLICY_VERSION)
    consolidation_store.record_reasoning_episode(
        job_id=job_id,
        memory_id=memory_id,
        episode=episode,
        policy_name=POLICY_NAME,
        policy_version=POLICY_VERSION,
        approval_state=approval_state,
        supersedes=None,
    )

    return SessionItemResult(
        "episode", True,
        f"Staged as `{memory_id}` ({approval_state}).",
        memory_id=memory_id,
    )


def _capture_doc_item(
    item: dict[str, Any], wiki_root: Optional[Any] = None, proposals_dir: Optional[Any] = None
) -> SessionItemResult:
    target_path = (item.get("target_path") or "").strip()
    proposed_content = item.get("proposed_content") or ""
    if not target_path or not proposed_content.strip():
        return SessionItemResult(
            "doc_proposal", False, "",
            error="'target_path' and 'proposed_content' are required for destination='doc_proposal'.",
        )
    rationale = (item.get("doc_rationale") or item.get("rationale") or "").strip()
    if not rationale:
        return SessionItemResult("doc_proposal", False, "", error="'doc_rationale' is required for destination='doc_proposal'.")

    try:
        proposal = create_doc_proposal(
            target_path=target_path,
            proposed_content=proposed_content,
            rationale=rationale,
            source_context=item.get("evidence_text"),
            wiki_root=wiki_root,
            proposals_dir=proposals_dir,
        )
    except (ValueError, RuntimeError) as e:
        # RuntimeError covers get_corpus_root() when no knowledge provider
        # is configured (LLM_WIKI_PATH unset) -- reported per-item like any
        # other bad input, never allowed to crash the rest of the batch.
        return SessionItemResult("doc_proposal", False, "", error=str(e))

    return SessionItemResult(
        "doc_proposal", True,
        f"Proposal `{proposal.proposal_id}` created for `{proposal.target_path}` ({proposal.operation}).",
        proposal_id=proposal.proposal_id,
    )


def capture_session(
    items: list[dict[str, Any]],
    project: Optional[str],
    source_description: Optional[str],
    session: Any,
    request_id: Any,
    db_path: Optional[Any] = None,
    wiki_root: Optional[Any] = None,
    proposals_dir: Optional[Any] = None,
) -> list[SessionItemResult]:
    """Route each item to record_reasoning_episode() or create_doc_proposal()
    per its own 'destination'. One bad item is reported, not fatal to the rest.

    `db_path`/`wiki_root`/`proposals_dir` are test-only -- production always
    uses the default paths, same convention PromotionStore/ConsolidationStore/
    create_doc_proposal already follow.
    """
    results: list[SessionItemResult] = []
    with ConsolidationStore(db_path=db_path) as consolidation_store:
        for item in items:
            destination = (item.get("destination") or "").strip()
            try:
                if destination == "episode":
                    result = _capture_episode_item(
                        item, project, source_description, session, request_id, consolidation_store, db_path=db_path
                    )
                elif destination == "doc_proposal":
                    result = _capture_doc_item(item, wiki_root=wiki_root, proposals_dir=proposals_dir)
                else:
                    result = SessionItemResult(
                        destination or "(missing)", False, "",
                        error=f"'destination' must be one of {VALID_DESTINATIONS}, got {destination!r}.",
                    )
            except Exception as e:  # noqa: BLE001 — one item's unexpected failure must not lose the rest
                logger.exception("capture_session: unexpected failure on item %r", item)
                result = SessionItemResult(destination or "(missing)", False, "", error=f"Unexpected error: {e}")
            results.append(result)
    return results


def format_capture_session_result(results: list[SessionItemResult]) -> str:
    lines = ["### Session capture results\n"]
    for i, r in enumerate(results, 1):
        if r.ok:
            lines.append(f"{i}. ✅ [{r.destination}] {r.detail}")
        else:
            lines.append(f"{i}. ❌ [{r.destination}] {r.error}")
    ok_count = sum(1 for r in results if r.ok)
    lines.append(f"\n{ok_count}/{len(results)} items captured.")
    return "\n".join(lines)
