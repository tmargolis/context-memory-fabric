"""Conversation-grouped review summary (found 2026-09-19, "review by
conversation").

Project-bucketing (server/review/projects.py) already answered "which
episodes belong to the same undertaking" for the personal corpus at large,
but Todd's MS4b batch (436 episodes + 69 doc proposals, all one project,
`context-memory-fabric`) showed project alone doesn't re-orient a reviewer
within a single project the way it does across a whole corpus -- every
item in that one bucket still needs re-reading from scratch. A
conversation/session is a tighter, already-coherent frame: everything from
one Claude Code session was one sitting, on one topic, and reviewing it as
a unit means loading that mental model once instead of per item.

This module answers "which conversations have pending items and how many"
-- the bucket-picking step an agent takes before listing one conversation's
items via list_episode_proposals(conversation_id=...) /
list_doc_proposals(conversation_id=...). It reads the same two read-only
projections those tools already use (the episode-proposals file mirror,
the doc-proposals file store) -- no new storage, no DB dependency.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from server.episode_proposals import list_episode_mirrors
from server.proposals import list_proposals


def list_review_conversations(
    harness: Optional[str] = None,
    policy_name: Optional[str] = None,
    include_tier2_only: bool = False,
    episode_base_dir: Optional[Path] = None,
    doc_proposals_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Aggregate pending (queued_for_review / pending_review) episode and
    doc-proposal counts by conversation_id, sorted by total pending
    descending (the busiest conversation first).

    Across the whole corpus this is hundreds of conversations, most with
    only tier-2 (out-of-review-scope) episodes left over from earlier
    passes -- found live 2026-09-19 running this unfiltered against
    production (482 conversations, almost all noise). Two ways to narrow
    it to one actual batch:

    - `harness`/`policy_name` filter the episode side by those exact
      fields (both are stored on every mirror). Doc proposals have no
      `policy_name` of their own -- only `harness` filters them -- so
      combining `policy_name` with a batch that also produced doc
      proposals from the same run still needs `harness` to catch those.
    - `include_tier2_only=False` (the default) drops any bucket whose
      total review-scope count (tier-1 episodes + doc proposals) is zero,
      since a conversation with only tier-2 leftovers isn't a "pending
      review" bucket, it's exhausted backlog noise. Pass True to see it
      anyway (e.g. auditing what's left over from an old pass).

    An item with no known conversation_id (a mirror/proposal written before
    that field existed, or one whose source conversation couldn't be
    resolved) is excluded from every bucket rather than pooled into a fake
    "unknown" bucket -- it stays reachable via the unfiltered
    list_episode_proposals/list_doc_proposals calls, just not grouped here.
    """
    buckets: dict[str, dict[str, Any]] = {}

    def _bucket(conversation_id: str, item_harness: Optional[str]) -> dict[str, Any]:
        b = buckets.get(conversation_id)
        if b is None:
            b = {
                "conversation_id": conversation_id,
                "harness": item_harness,
                "episodes_tier1": 0,
                "episodes_tier2": 0,
                "doc_proposals": 0,
                "doc_proposals_inferred": 0,
            }
            buckets[conversation_id] = b
        elif b["harness"] is None and item_harness is not None:
            b["harness"] = item_harness
        return b

    for ep in list_episode_mirrors(approval_state="queued_for_review", base_dir=episode_base_dir):
        conv = ep.get("conversation_id")
        if not conv:
            continue
        if harness is not None and ep.get("harness") != harness:
            continue
        if policy_name is not None and ep.get("policy_name") != policy_name:
            continue
        b = _bucket(conv, ep.get("harness"))
        if ep.get("tier") == "tier1":
            b["episodes_tier1"] += 1
        else:
            b["episodes_tier2"] += 1

    for p in list_proposals(status="pending_review", proposals_dir=doc_proposals_dir):
        if not p.source_conversation_id:
            continue
        if harness is not None and p.source_harness != harness:
            continue
        b = _bucket(p.source_conversation_id, p.source_harness)
        b["doc_proposals"] += 1
        if p.source_conversation_id_inferred:
            b["doc_proposals_inferred"] += 1

    result = list(buckets.values())
    if not include_tier2_only:
        result = [b for b in result if b["episodes_tier1"] + b["doc_proposals"] > 0]
    result.sort(key=lambda b: (b["episodes_tier1"] + b["doc_proposals"]), reverse=True)
    return result


def format_review_conversations(conversations: list[dict[str, Any]]) -> str:
    if not conversations:
        return "No conversations with pending items found."
    lines = [
        "| Conversation | Harness | Tier-1 Episodes | Tier-2 Episodes | Doc Proposals | Total (review-scope) |",
        "|---|---|---|---|---|---|",
    ]
    for c in conversations:
        review_scope = c["episodes_tier1"] + c["doc_proposals"]
        doc_note = f"{c['doc_proposals']}" + (f" ({c['doc_proposals_inferred']} inferred link)" if c["doc_proposals_inferred"] else "")
        lines.append(
            f"| `{c['conversation_id']}` | {c['harness'] or '?'} | {c['episodes_tier1']} | "
            f"{c['episodes_tier2']} | {doc_note} | {review_scope} |"
        )
    total_review_scope = sum(c["episodes_tier1"] + c["doc_proposals"] for c in conversations)
    lines.append(f"\n{len(conversations)} conversations, {total_review_scope} review-scope items total "
                 "(tier-1 episodes + doc proposals; tier-2 episodes shown for context, not counted).")
    return "\n".join(lines)
