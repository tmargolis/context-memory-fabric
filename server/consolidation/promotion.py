"""Promote auto-accepted derived memories into episodic memory (Graphiti).

The missing link between Milestone 3 (which stages a classification result
in `derived_memories` but deliberately never writes to Graphiti — see
pipeline.py's module docstring) and Milestone 6 (review/governance for
everything that ISN'T auto-accepted). MS3's exit gate defines
`auto_accepted` precisely so that category doesn't need a human in the
loop; this module is the explicit, auditable step that actually acts on
that design decision, deferred until the MS4a privacy/cost gate was
answered (it now is — see IMPLEMENTATION-PLAN.md's MS4a section).

Scope, deliberately narrow: only `approval_state == 'auto_accepted'` rows.
`queued_for_review` (the overwhelming majority — 9,658 of 19,012 events in
the production journal as of 2026-09-04) and `rejected` rows are untouched
here; they need Milestone 6's actual review tooling, not a promotion path
that bypasses review.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import logging
import sqlite3
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from server.consolidation.store import ConsolidationStore
from server.core.rate_limiter import GeminiQuotaExhaustedError
from server.journal.store import SqliteEventStore, DEFAULT_JOURNAL_PATH

logger = logging.getLogger(__name__)

RememberFn = Callable[..., Awaitable[dict[str, Any]]]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS promotions (
    memory_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    episode_name TEXT,
    graph_name TEXT,
    error TEXT,
    promoted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_promotions_status ON promotions(status);
"""


class PromotionStore:
    """Idempotency ledger for derived_memories -> Graphiti promotion.

    Lives in the same SQLite file as the journal and consolidation store
    (imports/journal/journal.db), same precedent as
    server.consolidation.store.ConsolidationStore's own module docstring:
    one file, foreign-key-shaped correctness by construction rather than
    cross-database consistency work.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_JOURNAL_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PromotionStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(self, memory_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM promotions WHERE memory_id = ?", (memory_id,)).fetchone()

    def is_promoted(self, memory_id: str) -> bool:
        row = self.get(memory_id)
        return row is not None and row["status"] == "succeeded"

    def record_success(self, memory_id: str, episode_name: str, graph_name: str) -> None:
        self._conn.execute(
            """
            INSERT INTO promotions (memory_id, status, episode_name, graph_name, error, promoted_at)
            VALUES (?, 'succeeded', ?, ?, NULL, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                status='succeeded', episode_name=excluded.episode_name,
                graph_name=excluded.graph_name, error=NULL, promoted_at=excluded.promoted_at
            """,
            (memory_id, episode_name, graph_name, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def record_failure(self, memory_id: str, error: str) -> None:
        self._conn.execute(
            """
            INSERT INTO promotions (memory_id, status, episode_name, graph_name, error, promoted_at)
            VALUES (?, 'failed', NULL, NULL, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET status='failed', error=excluded.error, promoted_at=excluded.promoted_at
            """,
            (memory_id, error, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def stats(self) -> dict[str, Any]:
        rows = self._conn.execute("SELECT status, COUNT(*) AS c FROM promotions GROUP BY status").fetchall()
        return {row["status"]: row["c"] for row in rows}


def _episode_name_for(memory_id: str, event_date: Optional[str]) -> str:
    date_part = (event_date or "undated")[:10].replace("-", "")
    short_hash = hashlib.sha256(memory_id.encode("utf-8")).hexdigest()[:8]
    return f"promoted_{date_part}_{short_hash}"


async def promote_auto_accepted(
    consolidation_store: ConsolidationStore,
    journal_store: SqliteEventStore,
    promotion_store: PromotionStore,
    remember_fn: RememberFn,
    dry_run: bool = True,
    limit: Optional[int] = None,
    graph_name: Optional[str] = None,
    inter_call_delay: float = 3.5,
) -> dict[str, Any]:
    """Promote `auto_accepted` derived_memories rows not yet promoted.

    Args:
        remember_fn: async callable matching server.memory.remember's
            signature (content, name, source_description, reference_time).
            Injected rather than imported directly so tests can supply a
            fake with no real Gemini/FalkorDB dependency.
        dry_run: if True (default), reports what would be promoted without
            calling remember_fn at all.
        limit: cap on how many rows to attempt in this call. A full
            promotion run is a real, minutes-long operation (each
            remember() call is rate-limited and gets a polite delay) —
            callers doing a first pass should pass a small limit.
        graph_name: recorded alongside each promotion for the report;
            informational only — remember_fn resolves the actual target
            graph itself (FALKORDB_DATABASE), this module never does.
        inter_call_delay: seconds to wait between successful remember()
            calls (default 3.5s, matching reconcile_memories' existing
            precedent). Pass 0 in tests to avoid real wall-clock delay.

    Returns:
        A summary dict: candidates considered, already_promoted (skipped),
        promoted (this run), failed (this run, with per-row errors), and
        stopped_early (True if a GeminiQuotaExhaustedError halted the run
        before considering every candidate — the remaining candidates are
        untouched, safe to retry in a later call).
    """
    rows = consolidation_store.query_derived_memories(approval_state="auto_accepted")

    candidates: list[sqlite3.Row] = []
    already_promoted = 0
    for row in rows:
        if promotion_store.is_promoted(row["memory_id"]):
            already_promoted += 1
            continue
        candidates.append(row)

    if limit is not None:
        candidates = candidates[:limit]

    result: dict[str, Any] = {
        "candidates_considered": len(rows),
        "already_promoted": already_promoted,
        "eligible_this_run": len(candidates),
        "dry_run": dry_run,
        "promoted": [],
        "failed": [],
        "stopped_early": False,
    }

    if dry_run or not candidates:
        result["promoted_preview"] = [
            {"memory_id": r["memory_id"], "statement": r["statement"], "event_date": r["event_date"]} for r in candidates
        ]
        return result

    for row in candidates:
        memory_id = row["memory_id"]
        source_event = journal_store.get(row["source_event_id"])
        harness = source_event.source.harness if source_event else "unknown"

        episode_name = _episode_name_for(memory_id, row["event_date"])
        reference_time = datetime.fromisoformat(row["event_date"]) if row["event_date"] else None
        source_description = (
            f"Promoted from {harness} evidence via {row['policy_name']}@{row['policy_version']} "
            f"(memory_id={memory_id})"
        )

        try:
            await remember_fn(
                content=row["statement"],
                name=episode_name,
                source_description=source_description,
                reference_time=reference_time,
            )
            promotion_store.record_success(memory_id, episode_name, graph_name or "")
            result["promoted"].append({"memory_id": memory_id, "episode_name": episode_name})
        except GeminiQuotaExhaustedError as e:
            logger.warning(f"Promotion run stopped early — rate limiter exhausted: {e}")
            result["stopped_early"] = True
            break
        except Exception as e:
            logger.error(f"Failed to promote {memory_id}: {e}")
            promotion_store.record_failure(memory_id, str(e))
            result["failed"].append({"memory_id": memory_id, "error": str(e)})
            continue

        # Polite delay between remember() calls, matching the precedent in
        # server.providers.memory_graphiti.reconcile_memories. Configurable
        # (and zeroed in tests) so the delay doesn't leak into test runtime.
        if inter_call_delay > 0:
            await asyncio.sleep(inter_call_delay)

    return result


# --------------------------------------------------------------------------
# MS3.6 — reasoning-episode promotion. There is NO auto-accept for reasoning
# episodes (MS3.5 exit gate: model confidence does not separate keep from
# drop). Promotion is entirely explicit: a human (MS6 review) approves a
# list of memory_ids, `promote_reviewed` acts on exactly that list.
#
# `reasoning_kind` gives a routing *hint* only — which episodes a reviewer
# should look at first, not which get promoted without review.
# --------------------------------------------------------------------------
TIER1_KINDS = frozenset({"decision", "plan", "retrospective", "rejected_alternative"})


def default_tier(reasoning_kind: Optional[str]) -> int:
    """1 = worth a promotion-review look; 2 = work-journal (stays in its
    thread, not queued for promotion). Never a gate — MS6 review can move
    any episode between tiers."""
    return 1 if (reasoning_kind or "") in TIER1_KINDS else 2


def tier1_review_queue(
    consolidation_store: ConsolidationStore, promotion_store: PromotionStore, policy_version: str = "0.2"
) -> list[sqlite3.Row]:
    """Reasoning episodes a reviewer should triage for promotion: tier-1
    kind, not already promoted, not rejected/superseded."""
    rows = consolidation_store.query_reasoning_episodes(
        policy_version=policy_version,
        kinds=sorted(TIER1_KINDS),
        exclude_approval_states=["rejected", "superseded_by_reasoning"],
    )
    return [r for r in rows if not promotion_store.is_promoted(r["memory_id"])]


def _reasoning_source_description(row: sqlite3.Row, harness: str) -> str:
    import json as _json

    bits = [f"Promoted from {harness} via {row['policy_name']}@{row['policy_version']}"]
    if row["reasoning_kind"]:
        bits.append(f"reasoning_kind={row['reasoning_kind']}")
    ev = _json.loads(row["evidence_event_ids_json"] or "[]") if "evidence_event_ids_json" in row.keys() else []
    if ev:
        bits.append(f"evidence={len(ev)} turn(s): {','.join(ev[:6])}")
    bits.append(f"memory_id={row['memory_id']}")
    return " | ".join(bits)


async def promote_reviewed(
    consolidation_store: ConsolidationStore,
    journal_store: SqliteEventStore,
    promotion_store: PromotionStore,
    remember_fn: RememberFn,
    memory_ids: list[str],
    dry_run: bool = True,
    graph_name: Optional[str] = None,
    inter_call_delay: float = 3.5,
) -> dict[str, Any]:
    """Promote an explicit, human-approved list of `derived_memories` rows
    (any policy — reasoning-episode or heuristic) into episodic memory.

    Same idempotency ledger, per-row failure isolation and
    `GeminiQuotaExhaustedError` clean-stop as `promote_auto_accepted`. The
    only difference is the candidate set: exactly `memory_ids`, in order,
    rather than an `approval_state` query.
    """
    requested = list(dict.fromkeys(memory_ids))  # dedupe, keep order
    rows = consolidation_store.get_derived_memories(requested)
    found_ids = {r["memory_id"] for r in rows}

    candidates: list[sqlite3.Row] = []
    already_promoted = 0
    for row in rows:
        if promotion_store.is_promoted(row["memory_id"]):
            already_promoted += 1
            continue
        candidates.append(row)

    result: dict[str, Any] = {
        "requested": len(requested),
        "not_found": [m for m in requested if m not in found_ids],
        "already_promoted": already_promoted,
        "eligible_this_run": len(candidates),
        "dry_run": dry_run,
        "promoted": [],
        "failed": [],
        "stopped_early": False,
    }

    if dry_run or not candidates:
        result["promoted_preview"] = [
            {"memory_id": r["memory_id"], "reasoning_kind": r["reasoning_kind"],
             "statement": r["statement"], "event_date": r["event_date"]}
            for r in candidates
        ]
        return result

    for row in candidates:
        memory_id = row["memory_id"]
        source_event = journal_store.get(row["source_event_id"])
        harness = source_event.source.harness if source_event else "unknown"
        episode_name = _episode_name_for(memory_id, row["event_date"])
        reference_time = datetime.fromisoformat(row["event_date"]) if row["event_date"] else None

        try:
            await remember_fn(
                content=row["statement"],
                name=episode_name,
                source_description=_reasoning_source_description(row, harness),
                reference_time=reference_time,
            )
            promotion_store.record_success(memory_id, episode_name, graph_name or "")
            result["promoted"].append({"memory_id": memory_id, "episode_name": episode_name,
                                       "reasoning_kind": row["reasoning_kind"]})
        except GeminiQuotaExhaustedError as e:
            logger.warning(f"promote_reviewed stopped early — rate limiter exhausted: {e}")
            result["stopped_early"] = True
            break
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to promote {memory_id}: {e}")
            promotion_store.record_failure(memory_id, str(e))
            result["failed"].append({"memory_id": memory_id, "error": str(e)})
            continue

        if inter_call_delay > 0:
            await asyncio.sleep(inter_call_delay)

    return result


def format_promotion_report(result: dict[str, Any]) -> str:
    # handles both promote_auto_accepted (candidates_considered) and
    # promote_reviewed (requested / not_found) result shapes
    considered = result.get("candidates_considered", result.get("requested", 0))
    lines = [
        f"### 🎓 Memory Promotion Report ({'DRY RUN' if result['dry_run'] else 'COMMITTED'})",
        f"- **Candidates:** {considered}",
        f"- **Already promoted (skipped):** {result['already_promoted']}",
        f"- **Eligible this run:** {result['eligible_this_run']}",
    ]
    if result.get("not_found"):
        lines.append(f"- **Not found (bad id):** {len(result['not_found'])} — {', '.join(result['not_found'][:3])}")
    if result["dry_run"]:
        lines.append("")
        lines.append("#### Would promote:")
        for item in result.get("promoted_preview", []):
            lines.append(f"- `{item['memory_id']}` (date: `{item['event_date']}`): {item['statement'][:100]}")
    else:
        lines.append(f"- **Promoted:** {len(result['promoted'])}")
        lines.append(f"- **Failed:** {len(result['failed'])}")
        if result["stopped_early"]:
            lines.append("- ⚠️ **Stopped early** — rate limiter reported no headroom. Remaining candidates untouched; safe to re-run later.")
        if result["promoted"]:
            lines.append("")
            lines.append("#### Promoted:")
            for item in result["promoted"]:
                lines.append(f"- `{item['memory_id']}` → episode `{item['episode_name']}`")
        if result["failed"]:
            lines.append("")
            lines.append("#### Failed:")
            for item in result["failed"]:
                lines.append(f"- `{item['memory_id']}`: {item['error']}")
    return "\n".join(lines)
