"""`review_queue()` — the MS6 review queue, grouped by project bucket.

Returns structured dicts, never formatted strings: the same payload feeds
the CLI, the review artifact and (later, MS9) a web UI. Formatting belongs
to the surface.

Grouping is by `project`, not by thread — see server.review.projects for
why the thread is the wrong unit. Buckets are ordered by tier-1 density
(most promotable decisions per look), matching the MS6 plan's intent even
though the unit underneath it changed.

Tier here is `server.consolidation.promotion.default_tier`: tier 1 is
"worth a promotion-review look", tier 2 is work-journal that stays in its
thread. Tier 2 is *not* review scope — 972 episodes at ~24s each is 6.6
hours to decide things that are never promoted by design. It is included
in the payload only as a per-bucket count, so a reviewer can see the shape
of what they are deliberately not reading.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from server.consolidation.promotion import PromotionStore, default_tier
from server.policies.reasoning_episode_v1 import REASONING_POLICY_VERSION
from server.review.explain import parse_reason, resolve_evidence
from server.review.store import PENDING, ReviewStore

# Approval states that mean "already resolved upstream of review".
_EXCLUDED_STATES = ("rejected", "superseded_by_reasoning", "superseded_by_correction")


def review_queue(
    conn: sqlite3.Connection,
    review_store: ReviewStore,
    promotion_store: PromotionStore,
    policy_version: str = REASONING_POLICY_VERSION,
    policy_name: str = "reasoning-episode",
    tier: Optional[int] = 1,
    projects: Optional[list[str]] = None,
    harness: Optional[str] = None,
    include_reviewed: bool = False,
    include_evidence: bool = False,
    max_evidence_chars: int = 1200,
) -> dict[str, Any]:
    """Build the queue.

    `include_evidence` inlines the resolved journal turns for every episode.
    That is what the export uses: a verdict should never require a
    round-trip back to the database, because the round-trip is what makes
    a reviewer skip the evidence and guess instead.

    `policy_name` defaults to `'reasoning-episode'` for backward
    compatibility -- pass e.g. `policy_name="extract"` to build the queue
    for a different windowed-extraction policy's output instead (this was
    a hardcoded literal, filed in docs/plan-active.md's Backlog, confirmed
    as a second instance of the same bug class as tier1_review_queue's).
    """
    qmarks = ",".join("?" * len(_EXCLUDED_STATES))
    params: list[Any] = [policy_name, policy_version, *_EXCLUDED_STATES]
    clauses = [
        "policy_name = ?",
        "policy_version = ?",
        f"approval_state NOT IN ({qmarks})",
    ]
    if projects:
        clauses.append(f"project IN ({','.join('?' * len(projects))})")
        params.extend(projects)

    rows = conn.execute(
        f"SELECT * FROM derived_memories WHERE {' AND '.join(clauses)} ORDER BY event_date, created_at",
        params,
    ).fetchall()

    # harness lives on the event, not the derived memory
    harness_by_event: dict[str, str] = {}
    if harness or rows:
        ev_ids = list({r["source_event_id"] for r in rows})
        for chunk_start in range(0, len(ev_ids), 900):  # SQLite parameter ceiling
            chunk = ev_ids[chunk_start : chunk_start + 900]
            for e in conn.execute(
                f"SELECT event_id, harness FROM events WHERE event_id IN ({','.join('?' * len(chunk))})", chunk
            ):
                harness_by_event[e["event_id"]] = e["harness"]

    buckets: dict[str, dict[str, Any]] = {}
    counted_tier2 = 0
    for row in rows:
        row_tier = default_tier(row["reasoning_kind"])
        row_harness = harness_by_event.get(row["source_event_id"], "unknown")
        if harness and row_harness != harness:
            continue

        project = row["project"] or "misc"
        bucket = buckets.setdefault(
            project,
            {"project": project, "episodes": [], "tier1_count": 0, "tier2_count": 0, "reviewed_count": 0},
        )
        if row_tier != 1:
            bucket["tier2_count"] += 1
            counted_tier2 += 1
        else:
            bucket["tier1_count"] += 1

        if tier is not None and row_tier != tier:
            continue

        state = review_store.state_of(row["memory_id"])
        if state != PENDING:
            bucket["reviewed_count"] += 1
            if not include_reviewed:
                continue

        parsed = parse_reason(row["reason"])
        evidence_ids = json.loads(row["evidence_event_ids_json"] or "[]")
        episode: dict[str, Any] = {
            "memory_id": row["memory_id"],
            "statement": row["statement"],
            "reasoning_kind": row["reasoning_kind"],
            "question": parsed.get("question"),
            "rationale": parsed.get("rationale"),
            "alternatives": parsed.get("alternatives"),
            "status": parsed.get("status"),
            "thread_key": row["thread_key"],
            "project": project,
            "event_date": row["event_date"],
            "harness": row_harness,
            "confidence": row["confidence"],
            "tier": row_tier,
            "review_state": state,
            "promoted": promotion_store.is_promoted(row["memory_id"]),
            "evidence_count": len(evidence_ids),
        }
        if include_evidence:
            episode["evidence"] = resolve_evidence(conn, evidence_ids, max_chars=max_evidence_chars)
        bucket["episodes"].append(episode)

    ordered = sorted(buckets.values(), key=lambda b: (-b["tier1_count"], b["project"]))
    queued = sum(len(b["episodes"]) for b in ordered)
    return {
        "buckets": ordered,
        "bucket_count": len([b for b in ordered if b["episodes"]]),
        "episode_count": queued,
        "tier1_total": sum(b["tier1_count"] for b in ordered),
        "tier2_total": counted_tier2,
        "already_reviewed": sum(b["reviewed_count"] for b in ordered),
        "policy_version": policy_version,
        "tier_filter": tier,
    }
