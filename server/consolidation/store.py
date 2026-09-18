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
import logging
from pathlib import Path
import sqlite3
from typing import Any, Optional

from server.episode_proposals import write_episode_mirror
from server.journal.store import DEFAULT_JOURNAL_PATH
from server.policies.protocols import ExtractionCategory, ExtractionResult, ReasoningEpisode
from server.policies.reasoning_episode_v1 import REASONING_POLICY_VERSION

logger = logging.getLogger(__name__)

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
    reasoning_kind TEXT,
    evidence_event_ids_json TEXT,
    approval_state TEXT NOT NULL,
    supersedes TEXT,
    superseded_by TEXT,
    thread_key TEXT,
    project TEXT,
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
        # A caller-supplied db_path (every test that isolates itself passes
        # one) automatically isolates the episode-proposals mirror too, as a
        # sibling directory next to it -- no test file needs to know this
        # mirror exists to avoid polluting the real project's
        # episode-proposals/. None (production default) means "use the real
        # project-root episode-proposals/" (server.episode_proposals's own
        # CMF_STATE_DIR/project-root convention).
        self._episode_proposals_dir = Path(db_path).parent / "episode-proposals" if db_path is not None else None
        db_path = db_path if db_path is not None else DEFAULT_JOURNAL_PATH
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA_SQL)
        self._migrate(self._conn)
        self._conn.commit()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Additive, idempotent in-place migrations for DBs created before a
        column existed. SCHEMA_SQL covers fresh DBs; this covers the journal
        file already on disk from an earlier milestone.
        """
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(derived_memories)")}
        if "reasoning_kind" not in cols:  # ADR 0005 / MS3.5
            conn.execute("ALTER TABLE derived_memories ADD COLUMN reasoning_kind TEXT")
        if "evidence_event_ids_json" not in cols:  # ADR 0005 / MS3.5 — windowed episodes cite >1 event
            conn.execute("ALTER TABLE derived_memories ADD COLUMN evidence_event_ids_json TEXT")
        if "superseded_by" not in cols:  # MS3.6 — coverage-based auto-resolve of the heuristic review pile
            conn.execute("ALTER TABLE derived_memories ADD COLUMN superseded_by TEXT")
        # MS6 — the review queue groups by project bucket, and `thread_key` was
        # only ever serialised into the `reason` text (see record_consolidation).
        # Promoting both to real columns removes a regex-parse from every queue
        # read; server.review.projects.backfill() populates them.
        if "thread_key" not in cols:
            conn.execute("ALTER TABLE derived_memories ADD COLUMN thread_key TEXT")
        if "project" not in cols:
            conn.execute("ALTER TABLE derived_memories ADD COLUMN project TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_derived_memories_thread_key ON derived_memories(thread_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_derived_memories_project ON derived_memories(project)")

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

    def mark_succeeded_no_output(self, job_id: str) -> None:
        """A window (MS3.5) the policy legitimately produced zero episodes
        from. The job is done — recorded so it is not re-run — but there is
        no derived_memories row to point at.
        """
        self._conn.execute(
            "UPDATE consolidation_jobs SET status='succeeded', last_error=NULL, updated_at=? WHERE job_id=?",
            (datetime.now(timezone.utc).isoformat(), job_id),
        )
        self._conn.commit()

    def record_triaged_out(
        self, job_id: str, source_event_id: str, policy_name: str, policy_version: str, reason: str
    ) -> None:
        """A window the loose triage gate withheld from the model (MS3.5).
        Recorded with status 'triaged_out' and the reason, so it is
        inspectable and can be re-run with triage disabled — never a silent
        drop (ADR 0005 decision 2).
        """
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO consolidation_jobs (job_id, source_event_id, policy_name, policy_version, status, attempts, last_error, created_at, updated_at)
            VALUES (:job_id, :source_event_id, :policy_name, :policy_version, 'triaged_out', 0, :reason, :now, :now)
            ON CONFLICT(job_id) DO UPDATE SET status='triaged_out', last_error=:reason, updated_at=:now
            """,
            {
                "job_id": job_id,
                "source_event_id": source_event_id,
                "policy_name": policy_name,
                "policy_version": policy_version,
                "reason": reason,
                "now": now,
            },
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
        reasoning_kind: Optional[str] = None,
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
                reasoning_kind, approval_state, supersedes, created_at
            ) VALUES (
                :memory_id, :source_event_id, :policy_name, :policy_version, :category,
                :statement, :reason, :confidence, :event_date, :date_precision,
                :reasoning_kind, :approval_state, :supersedes, :created_at
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
                "reasoning_kind": reasoning_kind if reasoning_kind is not None else getattr(result, "reasoning_kind", None),
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

    def record_reasoning_episode(
        self,
        job_id: str,
        memory_id: str,
        episode: ReasoningEpisode,
        policy_name: str,
        policy_version: str,
        approval_state: str,
        supersedes: Optional[str],
    ) -> None:
        """Write one windowed reasoning episode (MS3.5) as a derived_memories
        row and mark its job succeeded, in one transaction — same
        single-write-path guarantee as record_consolidation().

        `source_event_id` is the episode's primary (first cited) evidence
        event; the full list is kept in `evidence_event_ids_json` since a
        windowed episode rests on several turns. `reason` is composed from
        the episode's structured reasoning fields so the staging row is
        readable without re-joining anything.
        """
        now = datetime.now(timezone.utc).isoformat()
        evidence = list(episode.evidence_event_ids)
        primary = evidence[0] if evidence else ""
        reason_bits = [f"reasoning_kind={episode.reasoning_kind}"]
        if episode.driving_question:
            reason_bits.append(f"Q: {episode.driving_question}")
        if episode.rationale:
            reason_bits.append(f"why: {episode.rationale}")
        if episode.alternatives:
            reason_bits.append(f"alt: {episode.alternatives}")
        if episode.status:
            reason_bits.append(f"status={episode.status}")
        if episode.thread_key:
            reason_bits.append(f"thread={episode.thread_key}")

        self._conn.execute(
            """
            INSERT INTO derived_memories (
                memory_id, source_event_id, policy_name, policy_version, category,
                statement, reason, confidence, event_date, date_precision,
                reasoning_kind, evidence_event_ids_json, approval_state, supersedes, created_at
            ) VALUES (
                :memory_id, :source_event_id, :policy_name, :policy_version, :category,
                :statement, :reason, :confidence, :event_date, :date_precision,
                :reasoning_kind, :evidence_event_ids_json, :approval_state, :supersedes, :created_at
            )
            ON CONFLICT(memory_id) DO NOTHING
            """,
            {
                "memory_id": memory_id,
                "source_event_id": primary,
                "policy_name": policy_name,
                "policy_version": policy_version,
                "category": episode.category.value,
                "statement": episode.statement,
                "reason": " | ".join(reason_bits),
                "confidence": episode.confidence,
                "event_date": episode.event_date.isoformat() if episode.event_date else None,
                "date_precision": episode.date_precision.value,
                "reasoning_kind": episode.reasoning_kind,
                "evidence_event_ids_json": json.dumps(evidence),
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

        # Read-only file mirror (Backlog, "Proposal-directory housekeeping,"
        # 2026-09-18) -- best-effort, never allowed to fail a real staging
        # write that already committed above.
        try:
            write_episode_mirror(
                memory_id=memory_id,
                reasoning_kind=episode.reasoning_kind,
                statement=episode.statement,
                confidence=episode.confidence,
                evidence_event_ids=evidence,
                policy_name=policy_name,
                policy_version=policy_version,
                approval_state=approval_state,
                driving_question=episode.driving_question,
                rationale=episode.rationale,
                thread_key=episode.thread_key,
                base_dir=self._episode_proposals_dir,
            )
        except OSError:
            logger.exception("episode-proposals mirror write failed for %s (non-fatal)", memory_id)

    def stats(self) -> dict[str, Any]:
        total = self._conn.execute("SELECT COUNT(*) FROM derived_memories").fetchone()[0]
        by_category = dict(self._conn.execute("SELECT category, COUNT(*) FROM derived_memories GROUP BY category").fetchall())
        by_approval = dict(
            self._conn.execute("SELECT approval_state, COUNT(*) FROM derived_memories GROUP BY approval_state").fetchall()
        )
        by_job_status = dict(self._conn.execute("SELECT status, COUNT(*) FROM consolidation_jobs GROUP BY status").fetchall())
        by_reasoning_kind = dict(
            self._conn.execute(
                "SELECT reasoning_kind, COUNT(*) FROM derived_memories WHERE reasoning_kind IS NOT NULL GROUP BY reasoning_kind"
            ).fetchall()
        )
        return {
            "total_derived_memories": total,
            "by_category": by_category,
            "by_approval_state": by_approval,
            "by_job_status": by_job_status,
            "by_reasoning_kind": by_reasoning_kind,
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

    def get_derived_memories(self, memory_ids: list[str]) -> list[sqlite3.Row]:
        """Fetch specific rows by memory_id, preserving the input order
        (used by MS3.6 promote_reviewed for a human-approved id list)."""
        if not memory_ids:
            return []
        qmarks = ",".join("?" * len(memory_ids))
        rows = {
            r["memory_id"]: r
            for r in self._conn.execute(
                f"SELECT * FROM derived_memories WHERE memory_id IN ({qmarks})", memory_ids
            ).fetchall()
        }
        return [rows[m] for m in memory_ids if m in rows]

    def query_reasoning_episodes(
        self, policy_version: str, kinds: Optional[list[str]] = None, exclude_approval_states: Optional[list[str]] = None
    ) -> list[sqlite3.Row]:
        """Reasoning-episode rows for one policy version, optionally filtered
        to a set of `reasoning_kind`s (MS3.6 tier routing)."""
        clauses = ["policy_name = 'reasoning-episode'", "policy_version = ?"]
        params: list[Any] = [policy_version]
        if kinds:
            clauses.append(f"reasoning_kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if exclude_approval_states:
            clauses.append(f"approval_state NOT IN ({','.join('?' * len(exclude_approval_states))})")
            params.extend(exclude_approval_states)
        return self._conn.execute(
            f"SELECT * FROM derived_memories WHERE {' AND '.join(clauses)} ORDER BY created_at", params
        ).fetchall()

    def mark_superseded_by_reasoning(
        self, heuristic_version: str, reasoning_version: str = REASONING_POLICY_VERSION, dry_run: bool = False
    ) -> dict[str, Any]:
        """MS3.6 coverage-based auto-resolve: a heuristic `queued_for_review`
        row whose `source_event_id` is already cited by a reasoning episode's
        evidence is flipped to `approval_state='superseded_by_reasoning'`,
        with `superseded_by` set to that episode's memory_id. Review then
        only faces heuristic turns no reasoning episode claimed.

        Idempotent (rows already superseded are skipped) and reversible
        (`revert_superseded_by_reasoning`). Returns counts + a small sample.
        """
        # event_id -> a reasoning episode memory_id that covers it
        cover: dict[str, str] = {}
        for r in self._conn.execute(
            "SELECT memory_id, evidence_event_ids_json FROM derived_memories "
            "WHERE policy_name='reasoning-episode' AND policy_version=?",
            (reasoning_version,),
        ):
            for ev_id in json.loads(r["evidence_event_ids_json"] or "[]"):
                cover.setdefault(ev_id, r["memory_id"])

        targets = self._conn.execute(
            "SELECT memory_id, source_event_id, statement FROM derived_memories "
            "WHERE policy_name='heuristic-pattern' AND policy_version=? AND approval_state='queued_for_review'",
            (heuristic_version,),
        ).fetchall()

        hits = [(t["memory_id"], cover[t["source_event_id"]], t["statement"]) for t in targets if t["source_event_id"] in cover]
        now = None
        if not dry_run:
            now = datetime.now(timezone.utc).isoformat()
            for mid, by, _ in hits:
                self._conn.execute(
                    "UPDATE derived_memories SET approval_state='superseded_by_reasoning', superseded_by=?, created_at=created_at "
                    "WHERE memory_id=? AND approval_state='queued_for_review'",
                    (by, mid),
                )
            self._conn.commit()
        return {
            "heuristic_queued_before": len(targets),
            "covered_by_reasoning": len(hits),
            "remaining_queued": len(targets) - len(hits),
            "dry_run": dry_run,
            "sample": [{"heuristic": h, "superseded_by": b, "statement": s[:120]} for h, b, s in hits[:8]],
            "applied_at": now,
        }

    def record_correction(
        self, old_memory_id: str, new_memory_id: str, new_statement: str, reviewer: str, reason: str
    ) -> dict[str, Any]:
        """MS6b `correct_memory`'s journal-side counterpart: a corrected
        statement is a new derivation, not an overwrite of the old one —
        the same convention `mark_superseded_by_reasoning` uses. Copies the
        old row's classification (category, reasoning_kind, confidence,
        event_date, date_precision, evidence, project, thread_key) since
        only the statement text changed; `policy_name`/`policy_version`
        stay whatever originally derived it, so lineage queries by policy
        are unaffected by a later correction.

        The old row moves to `approval_state='superseded_by_correction'`
        (excluded from the review queue and promotion eligibility, same as
        `superseded_by_reasoning`) with `superseded_by` pointing at the new
        memory_id. The new row starts `queued_for_review` — matching every
        other reasoning-episode row — with `supersedes` pointing back.

        Raises if `old_memory_id` doesn't exist. One transaction.
        """
        old = self.get_derived_memory(old_memory_id)
        if old is None:
            raise ValueError(f"no derived_memories row for memory_id={old_memory_id!r}")

        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO derived_memories (
                memory_id, source_event_id, policy_name, policy_version, category,
                statement, reason, confidence, event_date, date_precision,
                reasoning_kind, evidence_event_ids_json, approval_state, supersedes,
                thread_key, project, created_at
            ) VALUES (
                :memory_id, :source_event_id, :policy_name, :policy_version, :category,
                :statement, :reason, :confidence, :event_date, :date_precision,
                :reasoning_kind, :evidence_event_ids_json, 'queued_for_review', :supersedes,
                :thread_key, :project, :created_at
            )
            """,
            {
                "memory_id": new_memory_id,
                "source_event_id": old["source_event_id"],
                "policy_name": old["policy_name"],
                "policy_version": old["policy_version"],
                "category": old["category"],
                "statement": new_statement,
                "reason": f"{old['reason']} | corrected by {reviewer}: {reason}",
                "confidence": old["confidence"],
                "event_date": old["event_date"],
                "date_precision": old["date_precision"],
                "reasoning_kind": old["reasoning_kind"],
                "evidence_event_ids_json": old["evidence_event_ids_json"],
                "supersedes": old_memory_id,
                "thread_key": old["thread_key"],
                "project": old["project"],
                "created_at": now,
            },
        )
        self._conn.execute(
            "UPDATE derived_memories SET approval_state='superseded_by_correction', superseded_by=? WHERE memory_id=?",
            (new_memory_id, old_memory_id),
        )
        self._conn.commit()
        return {"old_memory_id": old_memory_id, "new_memory_id": new_memory_id, "applied_at": now}

    def revert_superseded_by_reasoning(self) -> int:
        """Put every `superseded_by_reasoning` heuristic row back to
        `queued_for_review` (clears `superseded_by`). Returns the count."""
        cur = self._conn.execute(
            "UPDATE derived_memories SET approval_state='queued_for_review', superseded_by=NULL "
            "WHERE approval_state='superseded_by_reasoning'"
        )
        self._conn.commit()
        return cur.rowcount
