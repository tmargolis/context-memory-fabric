"""Review state and the audit trail — MS6.

Two tables in the same SQLite file as the journal, consolidation and
promotion stores (the one-file precedent set by
server.consolidation.store's module docstring):

  `reviews`       current verdict per memory_id — last-writer-wins.
  `review_audit`  append-only. Never updated, never deleted.

**Every mutation goes through `ReviewStore.record()`.** That is the entire
design. MS6's plan noted that "audit-everything is easy to design and easy
to forget to enforce — route every mutation through one chokepoint, not
per-action discipline"; this module is that chokepoint, and the actions in
server.review.actions have no other way to write.

The chokepoint is not bookkeeping for its own sake. The MS6 review pile
includes ~25,900 heuristic-pattern rows that will be bulk-rejected without
individual review (25,844 of them predate the reasoning-extraction window
entirely, and the policy stored the *raw user turn* as the statement).
Rejecting 22,390 rows in one action is only defensible because the audit
row records the filter, the count and the prior-state histogram that
reverses it. Reversibility is what buys the right to act in bulk.

A bulk action writes one `review_audit` row, not one per memory — an audit
trail nobody can read is not an audit trail — while still writing per-row
`reviews` entries so the queue reflects the outcome row by row.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid
from typing import Any, Optional

from server.journal.store import DEFAULT_JOURNAL_PATH

# Verdicts a memory can hold.
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
DEFERRED = "deferred"
VALID_STATES = frozenset({PENDING, APPROVED, REJECTED, DEFERRED})

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reviews (
    memory_id TEXT PRIMARY KEY,
    review_state TEXT NOT NULL,
    tier INTEGER,
    reviewer TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    reason TEXT,
    prior_state_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_state ON reviews(review_state);

CREATE TABLE IF NOT EXISTS review_audit (
    audit_id TEXT PRIMARY KEY,
    memory_id TEXT,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    at TEXT NOT NULL,
    reason TEXT,
    prior_state_json TEXT,
    new_state_json TEXT,
    batch_id TEXT,
    affected_count INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_review_audit_memory ON review_audit(memory_id);
CREATE INDEX IF NOT EXISTS idx_review_audit_batch ON review_audit(batch_id);
CREATE INDEX IF NOT EXISTS idx_review_audit_at ON review_audit(at);
"""


class ReviewStore:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_JOURNAL_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ReviewStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get(self, memory_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM reviews WHERE memory_id = ?", (memory_id,)).fetchone()

    def state_of(self, memory_id: str) -> str:
        row = self.get(memory_id)
        return row["review_state"] if row else PENDING

    def audit_for(self, memory_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM review_audit WHERE memory_id = ? ORDER BY at", (memory_id,)
        ).fetchall()

    def audit_batch(self, batch_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM review_audit WHERE batch_id = ? ORDER BY at", (batch_id,)
        ).fetchall()

    def counts(self) -> dict[str, int]:
        return {
            r["review_state"]: r["n"]
            for r in self._conn.execute("SELECT review_state, COUNT(*) n FROM reviews GROUP BY 1")
        }

    # ------------------------------------------------------------------
    # The chokepoint. Nothing else in server.review writes these tables.
    # ------------------------------------------------------------------
    def record(
        self,
        memory_id: str,
        action: str,
        new_state: str,
        reviewer: str,
        reason: Optional[str] = None,
        tier: Optional[int] = None,
        prior_state: Optional[dict[str, Any]] = None,
        batch_id: Optional[str] = None,
        commit: bool = True,
    ) -> str:
        """Apply one verdict and write its audit row, in one transaction.

        `prior_state` is captured from the existing `reviews` row when the
        caller does not supply one, so the audit trail always carries what
        the verdict replaced — that is what makes a mutation reversible.
        Returns the audit_id.
        """
        if new_state not in VALID_STATES:
            raise ValueError(f"invalid review_state {new_state!r}; expected one of {sorted(VALID_STATES)}")

        now = datetime.now(timezone.utc).isoformat()
        audit_id = uuid.uuid4().hex

        if prior_state is None:
            existing = self.get(memory_id)
            prior_state = dict(existing) if existing else {"review_state": PENDING}

        self._conn.execute(
            """
            INSERT INTO reviews (memory_id, review_state, tier, reviewer, reviewed_at, reason, prior_state_json)
            VALUES (:memory_id, :review_state, :tier, :reviewer, :reviewed_at, :reason, :prior_state_json)
            ON CONFLICT(memory_id) DO UPDATE SET
                review_state=:review_state, tier=:tier, reviewer=:reviewer,
                reviewed_at=:reviewed_at, reason=:reason, prior_state_json=:prior_state_json
            """,
            {
                "memory_id": memory_id,
                "review_state": new_state,
                "tier": tier,
                "reviewer": reviewer,
                "reviewed_at": now,
                "reason": reason,
                "prior_state_json": json.dumps(prior_state, default=str),
            },
        )
        self._conn.execute(
            """
            INSERT INTO review_audit (audit_id, memory_id, action, actor, at, reason,
                                      prior_state_json, new_state_json, batch_id, affected_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                audit_id,
                memory_id,
                action,
                reviewer,
                now,
                reason,
                json.dumps(prior_state, default=str),
                json.dumps({"review_state": new_state, "tier": tier}),
                batch_id,
            ),
        )
        if commit:
            self._conn.commit()
        return audit_id

    def note(
        self,
        memory_id: str,
        action: str,
        actor: str,
        reason: str,
        prior_state: dict[str, Any],
        new_state: dict[str, Any],
    ) -> str:
        """An audit-only entry for a mutation that isn't a review verdict.

        `expand_evidence` (server.review.actions) widens an episode's
        evidence_event_ids_json — a data-completeness fix, not a keep/drop
        decision — so it has no business upserting `reviews.review_state`.
        Routes through the same append-only `review_audit` table as every
        other mutation in this package, without touching `reviews` at all.
        """
        now = datetime.now(timezone.utc).isoformat()
        audit_id = uuid.uuid4().hex
        self._conn.execute(
            """
            INSERT INTO review_audit (audit_id, memory_id, action, actor, at, reason,
                                      prior_state_json, new_state_json, batch_id, affected_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1)
            """,
            (audit_id, memory_id, action, actor, now, reason,
             json.dumps(prior_state, default=str), json.dumps(new_state, default=str)),
        )
        self._conn.commit()
        return audit_id

    def record_bulk(
        self,
        memory_ids: list[str],
        action: str,
        new_state: str,
        reviewer: str,
        reason: str,
        prior_state_summary: dict[str, Any],
        tier: Optional[int] = None,
    ) -> dict[str, Any]:
        """A bulk verdict: per-row `reviews` entries, ONE `review_audit` row.

        `prior_state_summary` must describe what is being reversed — the
        filter that selected the rows and a histogram of their prior states.
        Per-row prior states are deliberately not stored: at 22k rows they
        would be a blob nobody reads, and the rows themselves are uniform.
        """
        if new_state not in VALID_STATES:
            raise ValueError(f"invalid review_state {new_state!r}")
        if not reason:
            raise ValueError("a bulk action requires a reason — it is the record that reverses it")

        now = datetime.now(timezone.utc).isoformat()
        batch_id = uuid.uuid4().hex
        audit_id = uuid.uuid4().hex

        self._conn.executemany(
            """
            INSERT INTO reviews (memory_id, review_state, tier, reviewer, reviewed_at, reason, prior_state_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                review_state=excluded.review_state, tier=excluded.tier, reviewer=excluded.reviewer,
                reviewed_at=excluded.reviewed_at, reason=excluded.reason, prior_state_json=excluded.prior_state_json
            """,
            [
                (mid, new_state, tier, reviewer, now, reason, json.dumps({"batch_id": batch_id}))
                for mid in memory_ids
            ],
        )
        self._conn.execute(
            """
            INSERT INTO review_audit (audit_id, memory_id, action, actor, at, reason,
                                      prior_state_json, new_state_json, batch_id, affected_count)
            VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                action,
                reviewer,
                now,
                reason,
                json.dumps(prior_state_summary, default=str),
                json.dumps({"review_state": new_state, "tier": tier}),
                batch_id,
                len(memory_ids),
            ),
        )
        self._conn.commit()
        return {"batch_id": batch_id, "audit_id": audit_id, "affected": len(memory_ids)}
