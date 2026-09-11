"""`correct_memory` / `delete_memory` — MS6b governance actions on promoted memories.

Both act on an already-promoted `derived_memories` row, found through
`PromotionStore` (memory_id -> episode_name in one graph) — not through
`edit_memory`'s free-text search over the whole graph
(server/providers/memory_graphiti.py). That distinction is the point: these
two operate on the memory_id a review verdict was recorded against, so the
audit trail stays anchored to the same identity used everywhere else in
server.review.

**Correction is fiddly because Graphiti has no in-place update.** There is
no API to hand it new text for an existing episode and have it re-derive
entities/edges in place. So `correct_memory` is remove_episode(old) +
add_episode(new) under the SAME `reference_time` the original episode
carried — the corrected statement keeps the temporal position the wrong
one held — with a fresh episode name, since a removed episode's name is
not a reusable identifier. Entities and edges are regenerated from the
corrected text, not patched: there is no cheaper path once the source text
changes.

**The journal side follows the same supersedes convention MS3 established**
for policy-version reprocessing (server.consolidation.store.
`record_correction`): a corrected statement is a *new* `derived_memories`
row linked via `supersedes`/`superseded_by`, never an overwrite of the
original memory_id's `statement`. The old memory_id moves to
`approval_state='superseded_by_correction'`; `PromotionStore`'s graph
mapping moves from the old memory_id to the new one (the old memory_id no
longer resolves in the graph — `explain()` follows `superseded_by` to the
new one); the new memory_id gets its own `reviews` row (`approved`, same
reviewer) so a promoted memory_id always has a review verdict to answer to
— the invariant Task 1 of MS6b confirmed holds for the whole graph today.

**Deletion is promotion's audited inverse.** It removes the Graphiti
episode and deletes the `PromotionStore` row so `promote_reviewed`'s
idempotency check no longer reports it promoted, but leaves
`derived_memories` and the journal untouched — this takes back a graph
presence, it does not un-happen the reviewed event. Re-promoting the same
memory_id is the recovery path if a deletion turns out to be wrong.

Every mutation here is recorded via `ReviewStore.note()`, not `.record()`:
neither changes a keep/drop verdict (that already happened at review time),
they change what exists in the graph — matching the precedent
`expand_evidence` set in server.review.actions.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Optional

from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType

from server.consolidation.promotion import PromotionStore
from server.consolidation.store import ConsolidationStore
from server.providers.memory_graphiti import parse_iso_datetime
from server.review.store import APPROVED, ReviewStore

logger = logging.getLogger(__name__)


def _records(rows: Any) -> list[dict[str, Any]]:
    recs = rows[0] if rows and isinstance(rows[0], list) else (rows or [])
    return [dict(r) for r in recs]


def _as_reference_time(valid_at: Any) -> Optional[datetime]:
    if valid_at is None:
        return None
    if isinstance(valid_at, datetime):
        return valid_at if valid_at.tzinfo else valid_at.replace(tzinfo=timezone.utc)
    return parse_iso_datetime(valid_at)


async def correct_memory(
    promotion_store: PromotionStore,
    review_store: ReviewStore,
    consolidation_store: ConsolidationStore,
    memory_id: str,
    new_content: str,
    graphiti: Graphiti,
    reviewer: str,
    reason: str,
    graph_name: Optional[str] = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Re-issue a promoted episode's content, preserving its reference_time,
    and record the correction in the journal as a superseding derivation.

    Args:
        new_content: the corrected episode body. Extraction re-runs against
            this text — entities/edges are not carried over from the old
            episode, since they may no longer be correct either.
        reason: required — this is what a future reviewer reads to
            understand why the graph's content diverges from what
            promotion originally wrote, and becomes part of the new
            journal row's `reason` field.
    """
    row = promotion_store.get(memory_id, graph_name)
    if row is None or row["status"] != "succeeded":
        return {"error": f"memory_id={memory_id!r} is not a successful promotion in this graph", "memory_id": memory_id}

    journal_row = consolidation_store.get_derived_memory(memory_id)
    if journal_row is None:
        return {"error": f"no derived_memories row for memory_id={memory_id!r} — cannot record a superseding entry", "memory_id": memory_id}

    old_episode_name = row["episode_name"]
    graph = row["graph_name"]
    driver = graphiti.driver

    episodes = _records(
        await driver.execute_query(
            "MATCH (e:Episodic {name: $name}) RETURN e.uuid AS uuid, e.valid_at AS valid_at, "
            "e.content AS content, e.source_description AS source_description",
            name=old_episode_name,
        )
    )
    if not episodes:
        return {"error": f"episode {old_episode_name!r} not found in graph {graph!r}", "memory_id": memory_id}
    episode = episodes[0]
    old_content = episode.get("content") or ""
    valid_at = episode.get("valid_at")
    source_description = episode.get("source_description") or ""

    if new_content == old_content:
        return {
            "dry_run": dry_run, "memory_id": memory_id,
            "note": "new_content is identical to the graph's current content; nothing to do",
        }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    new_episode_name = f"{old_episode_name}-corrected-{stamp}"
    new_memory_id = f"{memory_id}::corrected-{stamp}"

    if dry_run:
        return {
            "dry_run": True,
            "memory_id": memory_id,
            "new_memory_id": new_memory_id,
            "old_episode_name": old_episode_name,
            "new_episode_name": new_episode_name,
            "old_content": old_content,
            "new_content": new_content,
            "valid_at": valid_at,
        }

    reference_time = _as_reference_time(valid_at)
    await graphiti.remove_episode(episode["uuid"])
    await graphiti.add_episode(
        name=new_episode_name,
        episode_body=new_content,
        source_description=f"{source_description} | corrected by {reviewer}: {reason}",
        reference_time=reference_time or datetime.now(timezone.utc),
        source=EpisodeType.text,
    )

    # Graph identity moves to the new memory_id — the old one is superseded,
    # not promoted, and explain() follows `superseded_by` to reach the
    # current graph presence rather than resolving the old id directly.
    promotion_store.delete(memory_id, graph)
    promotion_store.record_success(new_memory_id, new_episode_name, graph)

    consolidation_store.record_correction(memory_id, new_memory_id, new_content, reviewer, reason)

    prior_review = review_store.get(memory_id)
    tier = prior_review["tier"] if prior_review is not None else None
    review_audit_id = review_store.record(
        new_memory_id, "correct_memory", APPROVED, reviewer,
        reason=f"correction of {memory_id}: {reason}", tier=tier,
        prior_state={"corrected_from": memory_id, "old_content": old_content},
    )
    graph_audit_id = review_store.note(
        memory_id, "correct_memory", reviewer, reason,
        prior_state={"episode_name": old_episode_name, "content": old_content},
        new_state={"episode_name": new_episode_name, "content": new_content, "new_memory_id": new_memory_id},
    )
    return {
        "dry_run": False,
        "memory_id": memory_id,
        "new_memory_id": new_memory_id,
        "old_episode_name": old_episode_name,
        "new_episode_name": new_episode_name,
        "graph_audit_id": graph_audit_id,
        "review_audit_id": review_audit_id,
    }


async def delete_memory(
    promotion_store: PromotionStore,
    review_store: ReviewStore,
    memory_id: str,
    graphiti: Graphiti,
    reviewer: str,
    reason: str,
    graph_name: Optional[str] = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Remove a promoted episode from the graph, leaving journal + derived_memories intact."""
    row = promotion_store.get(memory_id, graph_name)
    if row is None or row["status"] != "succeeded":
        return {"error": f"memory_id={memory_id!r} is not a successful promotion in this graph", "memory_id": memory_id}

    episode_name = row["episode_name"]
    graph = row["graph_name"]

    if dry_run:
        return {"dry_run": True, "memory_id": memory_id, "episode_name": episode_name, "graph_name": graph}

    driver = graphiti.driver
    episodes = _records(
        await driver.execute_query("MATCH (e:Episodic {name: $name}) RETURN e.uuid AS uuid", name=episode_name)
    )
    if episodes:
        await graphiti.remove_episode(episodes[0]["uuid"])
    else:
        logger.warning(
            f"delete_memory: episode {episode_name!r} already absent from graph {graph!r}; clearing ledger anyway"
        )

    promotion_store.delete(memory_id, graph)
    audit_id = review_store.note(
        memory_id, "delete_memory", reviewer, reason,
        prior_state={"episode_name": episode_name, "graph_name": graph},
        new_state={"promoted": False},
    )
    return {
        "dry_run": False,
        "memory_id": memory_id,
        "episode_name": episode_name,
        "graph_name": graph,
        "audit_id": audit_id,
    }
