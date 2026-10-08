"""Review actions — every one of them routed through ReviewStore.record*.

Deliberately *not* implemented, versus the MS6 plan:

  `approve_thread` / `reject_thread` — 73% of threads hold exactly one
  tier-1 episode, so a thread action is an episode action wearing extra
  machinery. The batching that actually helps is the project bucket, and
  that is a queue concern, not an action.

  `retier` — tier 2 is defined as "never promoted"; moving an episode to
  tier 1 and approving it is just approving it. `approve_episode` accepts
  any episode regardless of its default tier, which is the same capability
  with one fewer verb.

`approve_*` records the verdict; it does not call Graphiti. Promotion is a
separate, resumable step (`promote_approved`) because it makes rate-limited
network calls that can stop early on quota — a review pass must never be
held hostage to that, and a reviewer's 300 verdicts must not be lost
because call 41 exhausted the Gemini free tier.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import sqlite3
from typing import Any, Optional

from server.consolidation.promotion import PromotionStore, promote_reviewed
from server.consolidation.store import ConsolidationStore
from server.core.config import default_reviewer
from server.episode_proposals import move_episode_mirror
from server.journal.store import DEFAULT_JOURNAL_PATH, SqliteEventStore
from server.review.store import APPROVED, DEFERRED, REJECTED, ReviewStore

logger = logging.getLogger(__name__)

# Read once at import; server/__init__.py has already loaded .env by then.
DEFAULT_REVIEWER = default_reviewer()


def _episode_proposals_dir_for(review_store: ReviewStore) -> Optional[Path]:
    """Same db_path-derived isolation ConsolidationStore uses: a review_store
    pointed at a custom (e.g. test temp) db_path mirrors to a sibling
    episode-proposals/ dir next to it, never the real project's."""
    if review_store.db_path == DEFAULT_JOURNAL_PATH:
        return None
    return review_store.db_path.parent / "episode-proposals"


def _move_mirror_best_effort(
    review_store: ReviewStore, memory_id: str, new_status: str, reviewer: str, reason: Optional[str]
) -> None:
    """episode-proposals/ file mirror follow-through -- best-effort, never
    allowed to fail a real review verdict that already committed."""
    try:
        move_episode_mirror(
            memory_id, new_status, base_dir=_episode_proposals_dir_for(review_store),
            reviewer=reviewer, reason=reason,
        )
    except OSError:
        logger.exception("episode-proposals mirror move failed for %s -> %s (non-fatal)", memory_id, new_status)


def approve_episode(
    review_store: ReviewStore, memory_id: str, reviewer: str = DEFAULT_REVIEWER, reason: Optional[str] = None
) -> str:
    audit_id = review_store.record(memory_id, "approve_episode", APPROVED, reviewer, reason=reason, tier=1)
    _move_mirror_best_effort(review_store, memory_id, "approved", reviewer, reason)
    return audit_id


def reject_episode(
    review_store: ReviewStore, memory_id: str, reviewer: str = DEFAULT_REVIEWER, reason: Optional[str] = None
) -> str:
    audit_id = review_store.record(memory_id, "reject_episode", REJECTED, reviewer, reason=reason)
    _move_mirror_best_effort(review_store, memory_id, "rejected", reviewer, reason)
    return audit_id


def defer_episode(
    review_store: ReviewStore, memory_id: str, reviewer: str = DEFAULT_REVIEWER, reason: Optional[str] = None
) -> str:
    return review_store.record(memory_id, "defer_episode", DEFERRED, reviewer, reason=reason)


_VERDICT_ACTIONS = {APPROVED: approve_episode, REJECTED: reject_episode, DEFERRED: defer_episode}


def apply_verdicts(
    review_store: ReviewStore, verdicts: list[dict[str, Any]], reviewer: str = DEFAULT_REVIEWER
) -> dict[str, Any]:
    """Apply a batch of per-episode verdicts from the review surface.

    Each verdict is `{"memory_id": ..., "verdict": approved|rejected|deferred,
    "reason": optional}`. Unknown verdict values are collected and reported
    rather than raising — a surface round-trip should never fail wholesale
    because one row is malformed.
    """
    applied: dict[str, int] = {}
    errors: list[dict[str, str]] = []
    for v in verdicts:
        memory_id, verdict = v.get("memory_id"), v.get("verdict")
        fn = _VERDICT_ACTIONS.get(verdict or "")
        if not memory_id or fn is None:
            errors.append({"memory_id": str(memory_id), "error": f"bad verdict {verdict!r}"})
            continue
        fn(review_store, memory_id, reviewer=reviewer, reason=v.get("reason"))
        applied[verdict] = applied.get(verdict, 0) + 1
    return {"applied": applied, "total": sum(applied.values()), "errors": errors}


def expand_evidence(
    conn: sqlite3.Connection,
    review_store: ReviewStore,
    memory_id: str,
    additional_event_ids: list[str],
    reason: str,
    reviewer: str = DEFAULT_REVIEWER,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Widen an episode's evidence citation with connecting journal turns.

    A reasoning episode's `why` text sometimes references "turn N" — the
    extractor's own position within the window it processed, not a link to
    anything citable — while `evidence_event_ids_json` only ever names the
    single anchor turn the episode was built from. The turns that actually
    connect that anchor to the surrounding narrative are frequently never
    captured, which is exactly the shape that made
    openclaw-morning-briefing-overwrite's "turn 17" episode unreadable in
    isolation: three sibling episodes from one 144-turn Mac Pro setup
    conversation, each with exactly one evidence turn, sharing a thread_key
    that (separately) turned out to span five unrelated conversations too.

    Every `additional_event_ids` entry must exist and share the target
    episode's own source conversation_id — this cannot attach turns from a
    different conversation, which is the failure mode a "just add more
    evidence" tool would otherwise invite. Existing citations are never
    removed, only appended to, in citation order.

    Audited via `ReviewStore.note()`, not `record()`: this changes what
    evidence a reviewer sees, not a keep/drop verdict, so it has no reason
    to touch `reviews.review_state`.
    """
    row = conn.execute(
        "SELECT source_event_id, evidence_event_ids_json FROM derived_memories WHERE memory_id = ?", (memory_id,)
    ).fetchone()
    if row is None:
        return {"error": f"no derived_memories row for memory_id={memory_id!r}"}

    anchor_conv = conn.execute(
        "SELECT conversation_id FROM events WHERE event_id = ?", (row["source_event_id"],)
    ).fetchone()
    conv_id = anchor_conv["conversation_id"] if anchor_conv else None

    bad: list[str] = []
    for eid in additional_event_ids:
        e = conn.execute("SELECT conversation_id FROM events WHERE event_id = ?", (eid,)).fetchone()
        if e is None or e["conversation_id"] != conv_id:
            bad.append(eid)
    if bad:
        return {
            "error": "one or more event_ids do not exist or belong to a different conversation",
            "bad_event_ids": bad,
            "expected_conversation_id": conv_id,
        }

    prior = json.loads(row["evidence_event_ids_json"] or "[]")
    added = [e for e in additional_event_ids if e not in prior]
    merged = prior + added

    if dry_run or not added:
        return {"dry_run": True, "memory_id": memory_id, "prior_count": len(prior), "would_add": added}

    conn.execute(
        "UPDATE derived_memories SET evidence_event_ids_json = ? WHERE memory_id = ?",
        (json.dumps(merged), memory_id),
    )
    conn.commit()
    audit_id = review_store.note(
        memory_id, "expand_evidence", reviewer, reason,
        prior_state={"evidence_event_ids": prior},
        new_state={"evidence_event_ids": merged, "added": added},
    )
    return {"dry_run": False, "memory_id": memory_id, "prior_count": len(prior), "new_count": len(merged),
            "added": added, "audit_id": audit_id}


def bulk_reject(
    conn: sqlite3.Connection,
    review_store: ReviewStore,
    reason: str,
    policy_name: str = "heuristic-pattern",
    category: Optional[str] = None,
    approval_state: str = "queued_for_review",
    before_date: Optional[str] = None,
    reviewer: str = DEFAULT_REVIEWER,
    dry_run: bool = True,
    set_approval_state: bool = True,
) -> dict[str, Any]:
    """Reject a filtered population in one recorded, reversible action.

    This is what makes the ~25,900-row heuristic pile tractable. Those rows
    are not a queue of undecided candidates: `HeuristicPatternPolicyV1`
    stores the *raw user turn* as the statement, the `ambiguous` bucket has
    a mean confidence of 0.20, the `event_date` range runs 2000-2029, and
    25,844 of them predate the reasoning-extraction window entirely — they
    are the corpus the reasoning pass never ran on, not material it
    rejected. Reviewing them individually is ~36 hours to decide things
    like "how many steps in 1.4 MI".

    Defaults to `dry_run=True`. The audit row records the filter, the
    affected count and the prior-state histogram, which is what reverses
    it; `set_approval_state` also flips `derived_memories.approval_state`
    so existing queue reads agree with the review verdict.
    """
    clauses = ["policy_name = ?", "approval_state = ?"]
    params: list[Any] = [policy_name, approval_state]
    if category:
        clauses.append("category = ?")
        params.append(category)
    if before_date:
        # Strict: a row whose event_date is NULL is NOT known to fall before
        # the cutoff, so a date-scoped bulk action must leave it alone. The
        # production heuristic pile has dates spanning 2000-2029, some of
        # them plainly wrong, which is exactly why this must not guess.
        clauses.append("event_date IS NOT NULL AND event_date < ?")
        params.append(before_date)
    where = " AND ".join(clauses)

    rows = conn.execute(f"SELECT memory_id, approval_state FROM derived_memories WHERE {where}", params).fetchall()
    memory_ids = [r["memory_id"] for r in rows]

    prior_histogram: dict[str, int] = {}
    for r in rows:
        prior_histogram[r["approval_state"]] = prior_histogram.get(r["approval_state"], 0) + 1

    sample = [
        dict(r)
        for r in conn.execute(
            f"SELECT memory_id, statement, category, confidence, event_date FROM derived_memories "
            f"WHERE {where} ORDER BY RANDOM() LIMIT 10",
            params,
        )
    ]

    filter_spec = {
        "policy_name": policy_name,
        "category": category,
        "approval_state": approval_state,
        "before_date": before_date,
        "prior_state_histogram": prior_histogram,
        "matched": len(memory_ids),
    }

    if dry_run or not memory_ids:
        return {"dry_run": True, "matched": len(memory_ids), "filter": filter_spec, "sample": sample}

    result = review_store.record_bulk(
        memory_ids, "bulk_reject", REJECTED, reviewer, reason, prior_state_summary=filter_spec
    )
    if set_approval_state:
        conn.executemany(
            "UPDATE derived_memories SET approval_state = 'rejected' WHERE memory_id = ?",
            [(m,) for m in memory_ids],
        )
        conn.commit()
    return {"dry_run": False, "matched": len(memory_ids), "filter": filter_spec, "sample": sample, **result}


def bulk_reject_stale_policy_versions(
    conn: sqlite3.Connection,
    review_store: ReviewStore,
    policy_name: str = "heuristic-pattern",
    reviewer: str = DEFAULT_REVIEWER,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Retire queued rows that a later policy version already re-judged.

    The heuristic pile looks like ~26,000 pending judgements. It is not: the
    same turns were classified three times, under policy versions 1.0, 1.1
    and 1.2, and every re-classification was left queued alongside its
    predecessors. 25,961 queued rows cover only 9,757 distinct events, and
    16,303 of them carry an explicit `supersedes` pointer.

    So the first pass over the pile is bookkeeping, not review: for each
    source event, keep the row from the newest policy version and retire
    the rest. No sample audit is needed to justify it, because nothing is
    being judged — the retired rows are strictly older verdicts on a turn
    that a newer verdict already covers, and the newer row stays queued.
    """
    # The newest version per event must be computed across EVERY approval
    # state, not just the queued ones. A later policy version's row is
    # frequently no longer queued — MS3.6's coverage pass flips it to
    # `superseded_by_reasoning`, and auto-accept moves others — and scoping
    # this query to `queued_for_review` makes those rows invisible, so an
    # older version looks like the newest and its stale row survives for an
    # event that is already resolved.
    all_rows = conn.execute(
        "SELECT source_event_id, policy_version FROM derived_memories WHERE policy_name = ?",
        (policy_name,),
    ).fetchall()

    newest: dict[str, str] = {}
    for r in all_rows:
        key = r["source_event_id"]
        current = newest.get(key)
        if current is None or _version_key(r["policy_version"]) > _version_key(current):
            newest[key] = r["policy_version"]

    rows = conn.execute(
        "SELECT memory_id, source_event_id, policy_version, approval_state FROM derived_memories "
        "WHERE policy_name = ? AND approval_state = 'queued_for_review'",
        (policy_name,),
    ).fetchall()

    stale = [r for r in rows if r["policy_version"] != newest[r["source_event_id"]]]
    memory_ids = [r["memory_id"] for r in stale]
    stale_ids = set(memory_ids)

    by_version: dict[str, int] = {}
    for r in stale:
        by_version[r["policy_version"]] = by_version.get(r["policy_version"], 0) + 1

    summary = {
        "policy_name": policy_name,
        "approval_state": "queued_for_review",
        "prior_state_histogram": {"queued_for_review": len(memory_ids)},
        "stale_by_version": by_version,
        "kept_versions": sorted({r["policy_version"] for r in rows if r["memory_id"] not in stale_ids}),
        "distinct_events": len({r["source_event_id"] for r in rows}),
        "matched": len(memory_ids),
    }
    if dry_run or not memory_ids:
        return {"dry_run": True, **summary}

    result = review_store.record_bulk(
        memory_ids, "bulk_reject_stale_policy_versions", REJECTED, reviewer,
        f"superseded by a newer {policy_name} policy version for the same source event; "
        "the newer classification remains queued for review",
        prior_state_summary=summary,
    )
    conn.executemany(
        "UPDATE derived_memories SET approval_state = 'rejected' WHERE memory_id = ?",
        [(m,) for m in memory_ids],
    )
    conn.commit()
    return {"dry_run": False, **summary, **result}


def _version_key(version: str) -> tuple[int, ...]:
    """Sort policy versions numerically — '1.10' must outrank '1.9'."""
    try:
        return tuple(int(p) for p in str(version).split("."))
    except ValueError:
        return (0,)


def bulk_confirm_superseded(
    conn: sqlite3.Connection, review_store: ReviewStore, reviewer: str = DEFAULT_REVIEWER, dry_run: bool = True
) -> dict[str, Any]:
    """Confirm the `superseded_by_reasoning` rows without re-judging them.

    Each is already covered by a reasoning episode that is itself in the
    tier-1/tier-2 queue, so reviewing them is reviewing the same content
    twice. The MS6 plan wanted "one click rather than re-judge"; at 3,251
    rows that is still 3,251 clicks. One action, one audit row.
    """
    rows = conn.execute(
        "SELECT memory_id, superseded_by FROM derived_memories WHERE approval_state = 'superseded_by_reasoning'"
    ).fetchall()
    memory_ids = [r["memory_id"] for r in rows]
    if dry_run or not memory_ids:
        return {"dry_run": True, "matched": len(memory_ids)}

    result = review_store.record_bulk(
        memory_ids,
        "bulk_confirm_superseded",
        DEFERRED,
        reviewer,
        "covered by a reasoning episode already in the review queue (MS3.6 mark_superseded_by_reasoning); "
        "confirmed in bulk rather than re-judged",
        prior_state_summary={"approval_state": "superseded_by_reasoning", "matched": len(memory_ids)},
    )
    return {"dry_run": False, "matched": len(memory_ids), **result}


def revert_batch(
    conn: sqlite3.Connection, review_store: ReviewStore, batch_id: str,
    reviewer: str = DEFAULT_REVIEWER, dry_run: bool = True,
) -> dict[str, Any]:
    """Undo a bulk action, restoring the prior `approval_state`.

    The claim that a bulk reject is safe *because* it is reversible is only
    true if the reversal actually exists. This is it: the batch's audit row
    carries the filter and the prior-state histogram, and every row it
    touched carries the batch_id, so the population is exactly recoverable.

    A histogram with one prior state restores that state exactly. A mixed
    histogram cannot be inverted per-row from the summary alone, so the
    revert refuses rather than guessing — bulk actions in this module are
    always issued against a single `approval_state`, so a mixed histogram
    means something else wrote those rows and a human should look.
    """
    audit = review_store.audit_batch(batch_id)
    if not audit:
        return {"error": f"no batch {batch_id!r}", "reverted": 0}

    import json as _json

    prior = _json.loads(audit[0]["prior_state_json"] or "{}")
    histogram = prior.get("prior_state_histogram") or {}
    if len(histogram) != 1:
        return {
            "error": "prior state is not uniform; refusing to guess per-row state",
            "histogram": histogram,
            "reverted": 0,
        }
    restore_to = next(iter(histogram))

    rows = review_store.conn.execute(
        "SELECT memory_id FROM reviews WHERE prior_state_json LIKE ?", (f'%"{batch_id}"%',)
    ).fetchall()
    memory_ids = [r["memory_id"] for r in rows]
    if dry_run:
        return {"dry_run": True, "batch_id": batch_id, "would_revert": len(memory_ids), "restore_to": restore_to}

    conn.executemany(
        "UPDATE derived_memories SET approval_state = ? WHERE memory_id = ?",
        [(restore_to, m) for m in memory_ids],
    )
    conn.commit()
    review_store.conn.executemany("DELETE FROM reviews WHERE memory_id = ?", [(m,) for m in memory_ids])
    review_store.record_bulk(
        [], "revert_batch", DEFERRED, reviewer,
        f"reverted batch {batch_id} — {len(memory_ids)} rows restored to {restore_to!r}",
        prior_state_summary={"reverted_batch_id": batch_id, "restored_to": restore_to, "matched": len(memory_ids)},
    )
    review_store.conn.commit()
    return {"dry_run": False, "batch_id": batch_id, "reverted": len(memory_ids), "restore_to": restore_to}


def sample_audit(
    conn: sqlite3.Connection, batch_id: str, review_store: ReviewStore, n: int = 100
) -> list[dict[str, Any]]:
    """Draw a random sample from a bulk batch for the confirming audit.

    A bulk reject is only defensible if somebody actually looks at a sample
    of what it caught — this returns that sample, ready to read.
    """
    rows = conn.execute(
        "SELECT r.memory_id, d.statement, d.category, d.confidence, d.event_date "
        "FROM reviews r JOIN derived_memories d ON d.memory_id = r.memory_id "
        "WHERE r.prior_state_json LIKE ? ORDER BY RANDOM() LIMIT ?",
        (f'%"{batch_id}"%', n),
    ).fetchall()
    return [dict(r) for r in rows]


async def promote_approved(
    consolidation_store: ConsolidationStore,
    journal_store: SqliteEventStore,
    promotion_store: PromotionStore,
    review_store: ReviewStore,
    remember_fn: Any,
    dry_run: bool = True,
    graph_name: Optional[str] = None,
    limit: Optional[int] = None,
    inter_call_delay: Optional[float] = None,
    wait_through_rate_limit: bool = True,
    max_single_wait_seconds: float = 6 * 3600,
    tag_fn: Any = None,
    memory_id: Optional[str] = None,
) -> dict[str, Any]:
    """Promote everything currently `approved` and not already promoted.

    With `memory_id`, promotes only that one episode, under the same
    eligibility rules. This is the MCP tool's only mode: one Spark-local
    extraction takes 1–4 minutes, so a multi-episode call routinely outlives
    client tool-call timeouts (and overloads Spark). Bulk runs go through
    `python -m server.review.cli promote`.

    Thin wrapper over MS3.6's `promote_reviewed`, which already owns the
    idempotency ledger and per-row failure isolation. By default it now
    also waits out RPM-bound rate-limit stalls rather than aborting the
    whole batch — see `promote_reviewed`'s docstring for why. Re-running
    after a genuine stop (an RPD wall past `max_single_wait_seconds`, or
    `wait_through_rate_limit=False`) still resumes exactly where it left
    off either way, because approval lives in `reviews` and promotion
    lives in `promotions` — two ledgers, neither lost.

    Excludes memory_ids whose `derived_memories.approval_state` has since
    moved to `superseded_by_correction` / `superseded_by_reasoning` /
    `rejected` — a real bug found in production (2026-09-11): `reviews`
    is last-writer-wins per memory_id and `correct_memory` never touches
    it, so an old memory_id's original `approved` verdict from before a
    correction stays on record. `correct_memory` also clears the old
    memory_id's `PromotionStore` row (the graph identity moved to the new
    memory_id). Without this filter, that combination makes the OLD,
    since-corrected memory_id look freshly "approved and not yet
    promoted" and re-promotes its stale content — which is exactly what
    happened to the 360-cam/eclipse correction before this fix: the
    original wrong episode got silently re-created in the graph.
    """
    approved = [
        r["memory_id"]
        for r in review_store.conn.execute(
            """
            SELECT r.memory_id FROM reviews r
            LEFT JOIN derived_memories dm ON dm.memory_id = r.memory_id
            WHERE r.review_state = ?
              AND (dm.approval_state IS NULL OR dm.approval_state NOT IN
                   ('rejected', 'superseded_by_reasoning', 'superseded_by_correction'))
            ORDER BY r.reviewed_at
            """,
            (APPROVED,),
        )
    ]
    if memory_id is not None:
        if memory_id not in approved:
            return {
                "requested": 1,
                "eligible_this_run": 0,
                "dry_run": dry_run,
                "promoted": [],
                "failed": [],
                "stopped_early": False,
                "not_eligible": [memory_id],
                "note": "not an approved, un-superseded episode — check its review verdict",
            }
        approved = [memory_id]
    pending = [m for m in approved if not promotion_store.is_promoted(m)]
    if limit is not None:
        pending = pending[:limit]
    if not pending:
        return {
            "candidates_considered": len(approved),
            "already_promoted": len(approved),
            "eligible_this_run": 0,
            "dry_run": dry_run,
            "promoted": [],
            "failed": [],
            "stopped_early": False,
            "skipped": len(approved),
            "note": "nothing approved awaiting promotion",
        }

    return await promote_reviewed(
        consolidation_store,
        journal_store,
        promotion_store,
        remember_fn,
        memory_ids=pending,
        dry_run=dry_run,
        graph_name=graph_name,
        inter_call_delay=inter_call_delay,
        wait_through_rate_limit=wait_through_rate_limit,
        max_single_wait_seconds=max_single_wait_seconds,
        tag_fn=tag_fn,
    )
