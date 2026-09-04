"""Consolidation storage: derived memories and their processing jobs.

Lives in the same SQLite file as the event journal (server.journal.store),
not a separate database — derived memories only ever reference events by
event_id, and keeping them in one file makes "does this job's write
actually correspond to a real event" a trivial foreign-key-shaped fact
rather than a cross-database consistency problem.

Job semantics (Milestone 3 acceptance test 4, "killing the consolidator
mid-job loses no captured event and leaves no partial memory"): a job and
its derived_memory row are written in one transaction via
`record_consolidation()` — there is no code path that writes one without
the other. The source event itself lives in the separate, already
write-once `events` table (Milestone 2) and is never touched by anything
in this module, so it is structurally impossible for a failed
consolidation to damage captured evidence.

A job found in status='running' when a new consolidation run starts is
always retried rather than skipped: this pipeline runs synchronously in
one process, so a 'running' row can only mean a prior run crashed before
reaching record_consolidation() — there is no concurrent worker that could
still legitimately hold it. This assumption would need revisiting if a
genuinely concurrent/async consolidator is introduced (Milestone 4's
adapter traffic).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Optional

from server.journal.store import DEFAULT_JOURNAL_PATH
from server.policies.protocols import ExtractionCategory, ExtractionResult

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS derived_memories (
    memory_id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL,
    policy_name TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    category TEXT NOT NULL,
    statement TEXT NOT NULL,
    reason TEXT NOT NULL,
    confidence REAL NOT NULL,
    event_date TEXT,
    date_precision TEXT NOT NULL,
    approval_state TEXT NOT NULL,
    supersedes TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_derived_memories_source_event ON derived_memories(source_event_id);
CREATE INDEX IF NOT EXISTS idx_derived_memories_category ON derived_memories(category);
CREATE INDEX IF NOT EXISTS idx_derived_memories_approval_state ON derived_memories(approval_state);

CREATE TABLE IF NOT EXISTS consolidation_jobs (
    job_id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL,
    policy_name TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    derived_memory_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_consolidation_jobs_status ON consolidation_jobs(status);
"""


class ConsolidationStore:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        db_path = db_path if db_path is not None else DEFAULT_JOURNAL_PATH
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ConsolidationStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get_job(self, job_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM consolidation_jobs WHERE job_id = ?", (job_id,)).fetchone()

    def get_derived_memory(self, memory_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM derived_memories WHERE memory_id = ?", (memory_id,)).fetchone()

    def latest_derivation_for_event(self, source_event_id: str, exclude_memory_id: Optional[str] = None) -> Optional[sqlite3.Row]:
        """Most recent prior derivation for this event (any policy/version),
        used to populate `supersedes` when reprocessing under a new policy
        version. Excludes `exclude_memory_id` so a derivation never
        supersedes itself on a plain idempotent re-run.
        """
        rows = self._conn.execute(
            "SELECT * FROM derived_memories WHERE source_event_id = ? ORDER BY created_at DESC",
            (source_event_id,),
        ).fetchall()
        for row in rows:
            if row["memory_id"] != exclude_memory_id:
                return row
        return None

    def mark_running(self, job_id: str, source_event_id: str, policy_name: str, policy_version: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        existing = self.get_job(job_id)
        attempts = (existing["attempts"] + 1) if existing else 1
        self._conn.execute(
            """
            INSERT INTO consolidation_jobs (job_id, source_event_id, policy_name, policy_version, status, attempts, created_at, updated_at)
            VALUES (:job_id, :source_event_id, :policy_name, :policy_version, 'running', :attempts, :created_at, :updated_at)
            ON CONFLICT(job_id) DO UPDATE SET status='running', attempts=:attempts, updated_at=:updated_at
            """,
            {
                "job_id": job_id,
                "source_event_id": source_event_id,
                "policy_name": policy_name,
                "policy_version": policy_version,
                "attempts": attempts,
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now,
            },
        )
        self._conn.commit()

    def record_failure(self, job_id: str, error: str) -> None:
        self._conn.execute(
            "UPDATE consolidation_jobs SET status='failed', last_error=?, updated_at=? WHERE job_id=?",
            (error, datetime.now(timezone.utc).isoformat(), job_id),
        )
        self._conn.commit()

    def record_consolidation(
        self,
        job_id: str,
        memory_id: str,
        source_event_id: str,
        policy_name: str,
        policy_version: str,
        result: ExtractionResult,
        approval_state: str,
        supersedes: Optional[str],
    ) -> None:
        """Write the derived_memory row and mark its job succeeded, in one
        transaction. See module docstring — this is the only write path,
        by design, so a job can never exist without exactly the derived
        memory it produced (or vice versa).
        """
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO derived_memories (
                memory_id, source_event_id, policy_name, policy_version, category,
                statement, reason, confidence, event_date, date_precision,
                approval_state, supersedes, created_at
            ) VALUES (
                :memory_id, :source_event_id, :policy_name, :policy_version, :category,
                :statement, :reason, :confidence, :event_date, :date_precision,
                :approval_state, :supersedes, :created_at
            )
            ON CONFLICT(memory_id) DO NOTHING
            """,
            {
                "memory_id": memory_id,
                "source_event_id": source_event_id,
                "policy_name": policy_name,
                "policy_version": policy_version,
                "category": result.category.value,
                "statement": result.statement,
                "reason": result.reason,
                "confidence": result.confidence,
                "event_date": result.event_date.isoformat() if result.event_date else None,
                "date_precision": result.date_precision.value,
                "approval_state": approval_state,
                "supersedes": supersedes,
                "created_at": now,
            },
        )
        self._conn.execute(
            "UPDATE consolidation_jobs SET status='succeeded', derived_memory_id=?, updated_at=? WHERE job_id=?",
            (memory_id, now, job_id),
        )
        self._conn.commit()

    def stats(self) -> dict[str, Any]:
        total = self._conn.execute("SELECT COUNT(*) FROM derived_memories").fetchone()[0]
        by_category = dict(self._conn.execute("SELECT category, COUNT(*) FROM derived_memories GROUP BY category").fetchall())
        by_approval = dict(
            self._conn.execute("SELECT approval_state, COUNT(*) FROM derived_memories GROUP BY approval_state").fetchall()
        )
        by_job_status = dict(self._conn.execute("SELECT status, COUNT(*) FROM consolidation_jobs GROUP BY status").fetchall())
        return {
            "total_derived_memories": total,
            "by_category": by_category,
            "by_approval_state": by_approval,
            "by_job_status": by_job_status,
        }

    def query_derived_memories(
        self, category: Optional[str] = None, approval_state: Optional[str] = None
    ) -> list[sqlite3.Row]:
        clauses, params = [], []
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        if approval_state is not None:
            clauses.append("approval_state = ?")
            params.append(approval_state)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._conn.execute(f"SELECT * FROM derived_memories {where} ORDER BY created_at", params).fetchall()
