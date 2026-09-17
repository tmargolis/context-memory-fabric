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
import re
import sqlite3
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from server.consolidation.store import ConsolidationStore
from server.policies.reasoning_episode_v1 import REASONING_POLICY_VERSION
from server.core.rate_limiter import (
    GeminiQuotaExhaustedError,
    GeminiRateLimiter,
    get_default_rate_limiter,
    is_transient_gemini_error,
)
from server.journal.store import SqliteEventStore, DEFAULT_JOURNAL_PATH

logger = logging.getLogger(__name__)

RememberFn = Callable[..., Awaitable[dict[str, Any]]]

# Spacing between successive remember() calls in a batch run. On Gemini this
# is politeness toward a shared free-tier quota with a 15 RPM ceiling; the
# 3.5s value matches reconcile_memories' existing precedent. Local inference
# has no quota and no other tenant, so the delay is pure dead time: across a
# 1,243-episode backfill, 3.5s each adds over an hour of sleeping. Dropped to
# a token 0.2s rather than 0 so a runaway loop still yields.
GEMINI_INTER_CALL_DELAY = 3.5
LOCAL_INTER_CALL_DELAY = 0.2


def default_inter_call_delay() -> float:
    """Provider-appropriate spacing, resolved at call time.

    Resolved here rather than baked into the signatures so that flipping
    CMF_LLM_PROVIDER takes effect without editing three defaults, and so an
    explicit caller value (including 0, which the tests pass) still wins.
    """
    from server.core.config import load_config

    return LOCAL_INTER_CALL_DELAY if load_config().llm_is_local else GEMINI_INTER_CALL_DELAY


# Fixed backoff for a real Gemini API 429/503 that survives remember()'s own
# retry budget — see promote_reviewed's matching except block for why this
# is a flat constant rather than rate_limiter.seconds_until_headroom(): the
# local ledger already believed there was headroom (that's why the call was
# attempted), so asking it again would just repeat the same wrong answer.
# 65s rather than a bare 60s gives a small cushion past the RPM window's
# own boundary, matching the epsilon GeminiRateLimiter.seconds_until_headroom
# already adds for the same reason (landing exactly on the boundary is not
# reliably past it).
_API_TRANSIENT_ERROR_BACKOFF_SECONDS = 65.0

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS promotions (
    memory_id TEXT NOT NULL,
    status TEXT NOT NULL,
    episode_name TEXT,
    graph_name TEXT NOT NULL,
    error TEXT,
    promoted_at TEXT NOT NULL,
    PRIMARY KEY (memory_id, graph_name)
);
CREATE INDEX IF NOT EXISTS idx_promotions_status ON promotions(status);
CREATE INDEX IF NOT EXISTS idx_promotions_graph ON promotions(graph_name);
"""


def _configured_graph() -> str:
    """The graph this process is pointed at, for ledger scoping.

    Reads config rather than importing server.providers.memory_graphiti's
    resolve_target_database(), which would pull graphiti_core into every
    import of this module for a single string.
    """
    from server.core.config import load_config

    name = load_config().falkordb_database
    if not (name and name.strip()):
        raise MissingPromotionGraphError(
            "FALKORDB_DATABASE is not set, so promotions cannot be scoped to a graph. "
            "Set it in the project-root .env file."
        )
    return name.strip()


class MissingPromotionGraphError(RuntimeError):
    """Raised when a promotion cannot be attributed to a target graph."""


def _migrate_promotions_pk(conn: sqlite3.Connection) -> None:
    """Widen the promotions primary key from (memory_id) to (memory_id, graph_name).

    Without graph_name in the key, one memory can be recorded as promoted
    exactly once across all graphs -- so rebuilding into a second graph
    silently skips every row that ever succeeded anywhere, and the new graph
    comes up empty with a clean-looking ledger. That is precisely the
    Gemini-vs-local A/B this migration exists to enable.

    SQLite cannot alter a primary key in place, so this rebuilds the table.
    Idempotent: it inspects the existing key first and returns if already wide.
    """
    cols = conn.execute("PRAGMA table_info(promotions)").fetchall()
    if not cols:
        return  # fresh database; SCHEMA_SQL already created the wide key
    pk_cols = {c[1] for c in cols if c[5]}  # c[5] is the pk position, 0 when not part of it
    if "graph_name" in pk_cols:
        return

    backfill = _configured_graph()
    logger.info(
        "Migrating promotions to a (memory_id, graph_name) primary key; "
        "rows with no recorded graph are attributed to %r.",
        backfill,
    )
    conn.executescript(
        """
        CREATE TABLE promotions_migrated (
            memory_id TEXT NOT NULL,
            status TEXT NOT NULL,
            episode_name TEXT,
            graph_name TEXT NOT NULL,
            error TEXT,
            promoted_at TEXT NOT NULL,
            PRIMARY KEY (memory_id, graph_name)
        );
        """
    )
    conn.execute(
        """
        INSERT INTO promotions_migrated (memory_id, status, episode_name, graph_name, error, promoted_at)
        SELECT memory_id, status, episode_name,
               CASE WHEN graph_name IS NULL OR TRIM(graph_name) = '' THEN ? ELSE graph_name END,
               error, promoted_at
        FROM promotions
        """,
        (backfill,),
    )
    conn.executescript(
        """
        DROP TABLE promotions;
        ALTER TABLE promotions_migrated RENAME TO promotions;
        CREATE INDEX IF NOT EXISTS idx_promotions_status ON promotions(status);
        CREATE INDEX IF NOT EXISTS idx_promotions_graph ON promotions(graph_name);
        """
    )
    conn.commit()


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
        _migrate_promotions_pk(self._conn)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PromotionStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(self, memory_id: str, graph_name: Optional[str] = None) -> Optional[sqlite3.Row]:
        """The ledger row for this memory *in one graph*.

        `graph_name` defaults to the configured target, which makes the
        natural reading of every existing call site the correct one: "is this
        promoted into the graph I am working with", not "into any graph ever".
        """
        graph = graph_name or _configured_graph()
        return self._conn.execute(
            "SELECT * FROM promotions WHERE memory_id = ? AND graph_name = ?",
            (memory_id, graph),
        ).fetchone()

    def is_promoted(self, memory_id: str, graph_name: Optional[str] = None) -> bool:
        row = self.get(memory_id, graph_name)
        return row is not None and row["status"] == "succeeded"

    def graphs(self) -> list[str]:
        """Every graph this ledger has recorded a promotion into."""
        return [
            r["graph_name"]
            for r in self._conn.execute(
                "SELECT DISTINCT graph_name FROM promotions ORDER BY graph_name"
            ).fetchall()
        ]

    def max_semantic_seq(self, prefix: str, graph_name: Optional[str] = None) -> int:
        """Highest NNN already used for episode names `<prefix>-NNN` in one graph.

        Backs the `<harness>-<project>-NNN` episode naming in `promote_reviewed`.
        Reads the ledger (not the graph) so it stays correct across separate
        promotion runs; 0 when the bucket is empty.
        """
        graph = graph_name or _configured_graph()
        rows = self._conn.execute(
            "SELECT episode_name FROM promotions WHERE graph_name = ? AND episode_name LIKE ?",
            (graph, f"{prefix}-%"),
        ).fetchall()
        best = 0
        for r in rows:
            tail = (r["episode_name"] or "").rsplit("-", 1)[-1]
            if tail.isdigit():
                best = max(best, int(tail))
        return best

    def record_success(self, memory_id: str, episode_name: str, graph_name: str) -> None:
        self._conn.execute(
            """
            INSERT INTO promotions (memory_id, status, episode_name, graph_name, error, promoted_at)
            VALUES (?, 'succeeded', ?, ?, NULL, ?)
            ON CONFLICT(memory_id, graph_name) DO UPDATE SET
                status='succeeded', episode_name=excluded.episode_name,
                error=NULL, promoted_at=excluded.promoted_at
            """,
            (memory_id, episode_name, graph_name or _configured_graph(), datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def record_failure(self, memory_id: str, error: str, graph_name: Optional[str] = None) -> None:
        """Record a failed promotion, scoped to its target graph.

        graph_name is no longer nullable: it is half the primary key, and
        SQLite permits NULLs in a non-INTEGER primary key, so a NULL here
        would let the same failure be inserted repeatedly instead of updating.
        """
        self._conn.execute(
            """
            INSERT INTO promotions (memory_id, status, episode_name, graph_name, error, promoted_at)
            VALUES (?, 'failed', NULL, ?, ?, ?)
            ON CONFLICT(memory_id, graph_name) DO UPDATE SET
                status='failed', error=excluded.error, promoted_at=excluded.promoted_at
            """,
            (memory_id, graph_name or _configured_graph(), error, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def stats(self) -> dict[str, Any]:
        rows = self._conn.execute("SELECT status, COUNT(*) AS c FROM promotions GROUP BY status").fetchall()
        return {row["status"]: row["c"] for row in rows}

    def delete(self, memory_id: str, graph_name: Optional[str] = None) -> None:
        """Remove this memory's ledger row for one graph.

        The inverse of `record_success` — used by MS6b's `delete_memory` to
        take back a promotion. Once gone, `is_promoted()` reports False
        again, so `promote_reviewed` can re-promote from a clean review
        without the idempotency check silently skipping it.
        """
        graph = graph_name or _configured_graph()
        self._conn.execute("DELETE FROM promotions WHERE memory_id = ? AND graph_name = ?", (memory_id, graph))
        self._conn.commit()


def _episode_name_for(memory_id: str, event_date: Optional[str]) -> str:
    date_part = (event_date or "undated")[:10].replace("-", "")
    short_hash = hashlib.sha256(memory_id.encode("utf-8")).hexdigest()[:8]
    return f"promoted_{date_part}_{short_hash}"


def _slug(value: Optional[str]) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return s or "misc"


def _harness_of(memory_id: str) -> str:
    """Originating harness from a reasoning-episode memory_id
    (`reason:<conv>:<harness>:...`; gemini ids are `reason:<hex>:gemini:apps:...`)."""
    parts = memory_id.split(":")
    return _slug(parts[2]) if len(parts) > 2 else "unknown"


def _semantic_episode_name(
    memory_id: str,
    project: Optional[str],
    promotion_store: "PromotionStore",
    graph_name: Optional[str],
) -> str:
    """`<harness>-<project>-NNN`, e.g. `chatgpt-astrophotography-001`.

    NNN is the next free sequence for that bucket in the target graph's
    ledger — stable across runs, sequential within a run (each
    `record_success` lands before the next name is built). Falls back to
    `_episode_name_for` if the pieces are missing.
    """
    harness = _harness_of(memory_id)
    if harness == "unknown":
        return _episode_name_for(memory_id, None)
    prefix = f"{harness}-{_slug(project)}"
    return f"{prefix}-{promotion_store.max_semantic_seq(prefix, graph_name) + 1:03d}"


async def promote_auto_accepted(
    consolidation_store: ConsolidationStore,
    journal_store: SqliteEventStore,
    promotion_store: PromotionStore,
    remember_fn: RememberFn,
    dry_run: bool = True,
    limit: Optional[int] = None,
    graph_name: Optional[str] = None,
    inter_call_delay: Optional[float] = None,
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
                content=enriched_episode_content(row),
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
            promotion_store.record_failure(memory_id, str(e), graph_name)
            result["failed"].append({"memory_id": memory_id, "error": str(e)})
            continue

        # Polite delay between remember() calls, matching the precedent in
        # server.providers.memory_graphiti.reconcile_memories. Configurable
        # (and zeroed in tests) so the delay doesn't leak into test runtime.
        delay = inter_call_delay if inter_call_delay is not None else default_inter_call_delay()
        if delay > 0:
            await asyncio.sleep(delay)

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
    consolidation_store: ConsolidationStore, promotion_store: PromotionStore, policy_version: str = REASONING_POLICY_VERSION
) -> list[sqlite3.Row]:
    """Reasoning episodes a reviewer should triage for promotion: tier-1
    kind, not already promoted, not rejected/superseded."""
    rows = consolidation_store.query_reasoning_episodes(
        policy_version=policy_version,
        kinds=sorted(TIER1_KINDS),
        exclude_approval_states=["rejected", "superseded_by_reasoning", "superseded_by_correction"],
    )
    return [r for r in rows if not promotion_store.is_promoted(r["memory_id"])]


_REASON_QUESTION_RE = re.compile(r"Q: (.*?)(?: \| why:| \| alt:| \| status=| \| thread=|$)", re.DOTALL)
_REASON_RATIONALE_RE = re.compile(r"why: (.*?)(?: \| alt:| \| status=| \| thread=|$)", re.DOTALL)


def enriched_episode_content(row: sqlite3.Row) -> str:
    """The episode body actually sent to remember(), as of MS7b (2026-09-13).

    Previously every promotion call site sent `row["statement"]` alone.
    `derived_memories.reason` -- for reasoning-episode rows, `Q: ... | why:
    ... | alt: ... | status=... | thread=...` (see
    ConsolidationStore.record_reasoning_episode) -- was written to the
    journal but never reached the graph. Measured across the 465 episodes
    promoted before this change: statement averages 188 chars, reason 406;
    98% of rows carry both a driving question and a rationale. That's
    roughly two-thirds of the extracted reasoning discarded at this one
    call site, not at extraction.

    Appends the driving question and rationale when present. Alternatives
    are deliberately excluded (Todd, 2026-09-13): "options considered and
    rejected" reads as settled fact once it is sitting in a graph, not as
    the discarded option it was.

    Rows from a policy that doesn't write this Q:/why: shape (e.g.
    heuristic_v1's ExtractionResult.reason, used by promote_auto_accepted)
    simply match neither pattern and fall back to the statement alone --
    this never raises on an unfamiliar `reason` format.
    """
    statement = row["statement"]
    reason = row["reason"] if "reason" in row.keys() else None
    if not reason:
        return statement

    parts = [statement]
    q = _REASON_QUESTION_RE.search(reason)
    if q and q.group(1).strip():
        parts.append(f"Driving question: {q.group(1).strip()}")
    w = _REASON_RATIONALE_RE.search(reason)
    if w and w.group(1).strip():
        parts.append(f"Reasoning: {w.group(1).strip()}")
    return "\n".join(parts)


def _reasoning_source_description(row: sqlite3.Row, harness: str) -> str:
    import json as _json

    bits = [f"Promoted from {harness} via {row['policy_name']}@{row['policy_version']}"]
    if row["reasoning_kind"]:
        bits.append(f"reasoning_kind={row['reasoning_kind']}")
    # MS6's project taxonomy (server.review.projects), threaded through as
    # plain key=value text rather than Graphiti's group_id — group_id is a
    # hard partition boundary for entity resolution/search, and this fabric's
    # whole thesis is cross-cutting personal context; a per-project partition
    # would stop e.g. "Todd's Mac Pro" from resolving as one entity across
    # openclaw and career-navigator-dev. This is BM25-searchable free text,
    # nothing more, deliberately.
    if "project" in row.keys() and row["project"]:
        bits.append(f"project={row['project']}")
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
    inter_call_delay: Optional[float] = None,
    wait_through_rate_limit: bool = True,
    max_single_wait_seconds: float = 6 * 3600,
    rate_limiter: Optional[GeminiRateLimiter] = None,
) -> dict[str, Any]:
    """Promote an explicit, human-approved list of `derived_memories` rows
    (any policy — reasoning-episode or heuristic) into episodic memory.

    Same idempotency ledger and per-row failure isolation as
    `promote_auto_accepted`. The candidate set is exactly `memory_ids`, in
    order, rather than an `approval_state` query.

    `GeminiQuotaExhaustedError` handling, when `wait_through_rate_limit`
    (the default): sleep for exactly as long as
    `GeminiRateLimiter.seconds_until_headroom()` says is needed, then
    retry the SAME episode — it is never skipped or counted as failed for
    a quota stall. A real 283-episode run hit this in practice: the local
    ledger's rate estimate is deliberately conservative (`DEFAULT_CALLS_PER_OPERATION
    = 3` reserved per remember() call, since Graphiti's add_episode() may
    issue more than one underlying LLM call), so it can — and did — judge
    the chain "exhausted" well before the account's real dashboard showed
    a hard wall (19-20 RPM against a 15 RPM cap, not remotely a full-day
    block), and an RPM wall clears on its own within about a minute. The
    previous behavior (`stopped_early=True; break`) turned a ~60s wait
    into "abandon the rest of the batch, needs a manual re-run" — this is
    the fix. `max_single_wait_seconds` bounds any ONE wait (an RPD wall
    can be hours from a Pacific-midnight boundary) so a genuinely
    pathological config still stops instead of hanging indefinitely;
    normal RPM stalls never come close to it. Set
    `wait_through_rate_limit=False` to restore the old fail-fast behavior.
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

    result["waited_seconds"] = 0.0
    result["quota_stalls"] = 0
    result["api_stalls"] = 0
    if rate_limiter is None and wait_through_rate_limit:
        rate_limiter = get_default_rate_limiter()

    for row in candidates:
        memory_id = row["memory_id"]
        source_event = journal_store.get(row["source_event_id"])
        harness = source_event.source.harness if source_event else "unknown"
        episode_name = _semantic_episode_name(
            memory_id, row["project"], promotion_store, graph_name
        )
        reference_time = datetime.fromisoformat(row["event_date"]) if row["event_date"] else None

        row_waited = 0.0
        while True:
            try:
                await remember_fn(
                    content=enriched_episode_content(row),
                    name=episode_name,
                    source_description=_reasoning_source_description(row, harness),
                    reference_time=reference_time,
                )
                promotion_store.record_success(memory_id, episode_name, graph_name or "")
                result["promoted"].append({"memory_id": memory_id, "episode_name": episode_name,
                                           "reasoning_kind": row["reasoning_kind"]})
                break
            except GeminiQuotaExhaustedError as e:
                if not wait_through_rate_limit or row_waited >= max_single_wait_seconds:
                    logger.warning(f"promote_reviewed stopped early — rate limiter exhausted: {e}")
                    result["stopped_early"] = True
                    return result
                wait = rate_limiter.seconds_until_headroom()
                wait = min(wait, max_single_wait_seconds - row_waited)
                logger.warning(
                    f"Rate limiter exhausted for {memory_id}; waiting {wait:.0f}s for headroom, "
                    f"then retrying (not skipping, not counted as failed): {e}"
                )
                await asyncio.sleep(wait)
                row_waited += wait
                result["waited_seconds"] += wait
                result["quota_stalls"] += 1
                continue
            except Exception as e:  # noqa: BLE001
                # A real Gemini API 429/503 that survived remember()'s own
                # bounded retry budget (2-3 attempts) is NOT a
                # GeminiQuotaExhaustedError — that type is only raised by
                # this process's own LOCAL pre-emptive reservation check,
                # which believed there was headroom (that belief is exactly
                # why the call was allowed through in the first place). A
                # 283-episode run hit this directly: stopped_early stayed
                # False the whole run (the local ledger never blocked
                # pre-emptively) while 21 episodes still failed on genuine
                # 429/RESOURCE_EXHAUSTED responses from Google. Calling
                # rate_limiter.seconds_until_headroom() here would be
                # self-deceiving — the local ledger is the thing that was
                # just proven wrong — so this uses a fixed backoff instead,
                # long enough to cover a real RPM-shaped wall.
                if wait_through_rate_limit and is_transient_gemini_error(e) and row_waited < max_single_wait_seconds:
                    wait = min(_API_TRANSIENT_ERROR_BACKOFF_SECONDS, max_single_wait_seconds - row_waited)
                    logger.warning(
                        f"Real API-side transient error for {memory_id} survived remember()'s own "
                        f"retries; waiting {wait:.0f}s and retrying (not counted as failed): {e}"
                    )
                    await asyncio.sleep(wait)
                    row_waited += wait
                    result["waited_seconds"] += wait
                    result["api_stalls"] += 1
                    continue
                logger.error(f"Failed to promote {memory_id}: {e}")
                promotion_store.record_failure(memory_id, str(e), graph_name)
                result["failed"].append({"memory_id": memory_id, "error": str(e)})
                break

        delay = inter_call_delay if inter_call_delay is not None else default_inter_call_delay()
        if delay > 0:
            await asyncio.sleep(delay)

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
