"""Append-only SQLite event journal.

Durability semantics (Milestone 2 requirement): WAL journal mode plus
synchronous=FULL. WAL because it allows concurrent readers (the CLI, tests,
and a future consolidation worker) while a single writer appends, without
the whole-database lock plain rollback-journal mode would need. synchronous=
FULL rather than the WAL-typical NORMAL because this is evidence of record —
Milestone 2's "explicit durability semantics" requirement is read here as
"prefer a slower fsync over a small window where a confirmed write could be
lost to a power failure," which NORMAL does not guarantee (it only fsyncs
the WAL file at checkpoint, not on every commit). This is a deliberate
trade of write throughput for the guarantee that once append() returns, the
event has survived an OS crash, not just a process crash.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Optional

from server.core.models import DatePrecision, SourceEvent, SourceProvenance

DEFAULT_JOURNAL_PATH = Path(__file__).resolve().parent.parent.parent / "imports" / "journal" / "journal.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    event_type TEXT NOT NULL,
    harness TEXT NOT NULL,
    account_scope TEXT,
    conversation_id TEXT,
    session_id TEXT,
    turn_id TEXT,
    model TEXT,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    observed_at TEXT NOT NULL,
    event_date TEXT,
    date_precision TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    parent_event_ids_json TEXT NOT NULL DEFAULT '[]',
    attachment_refs_json TEXT NOT NULL DEFAULT '[]',
    privacy_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    retention_class TEXT NOT NULL DEFAULT 'raw',
    inserted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_harness ON events(harness);
CREATE INDEX IF NOT EXISTS idx_events_conversation ON events(conversation_id);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_actor ON events(actor_type, actor_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_observed_at ON events(observed_at);
CREATE INDEX IF NOT EXISTS idx_events_event_date ON events(event_date);
CREATE INDEX IF NOT EXISTS idx_events_content_hash ON events(content_hash);
"""


class SqliteEventStore:
    """Append-only EventStore (server.core.protocols.EventStore) backed by SQLite.

    Not thread-safe across processes writing concurrently beyond what
    SQLite's own WAL mode provides; this project has exactly one writer
    (an importer or the future consolidation worker) at a time by
    convention, not by an enforced lock.
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

    def __enter__(self) -> "SqliteEventStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def append(self, event: SourceEvent) -> bool:
        """Insert `event` if its event_id is not already present.

        Returns:
            True if a new row was inserted, False if event_id already
            existed (idempotent no-op — this is what makes "importing the
            same export twice produces zero new events" true).
        """
        row = _event_to_row(event)
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO events (
                event_id, schema_version, event_type, harness, account_scope,
                conversation_id, session_id, turn_id, model, actor_type, actor_id,
                observed_at, event_date, date_precision, content_json, content_hash,
                parent_event_ids_json, attachment_refs_json, privacy_json, metadata_json,
                retention_class, inserted_at
            ) VALUES (
                :event_id, :schema_version, :event_type, :harness, :account_scope,
                :conversation_id, :session_id, :turn_id, :model, :actor_type, :actor_id,
                :observed_at, :event_date, :date_precision, :content_json, :content_hash,
                :parent_event_ids_json, :attachment_refs_json, :privacy_json, :metadata_json,
                :retention_class, :inserted_at
            )
            """,
            row,
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get(self, event_id: str) -> Optional[SourceEvent]:
        cursor = self._conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,))
        row = cursor.fetchone()
        return _row_to_event(row) if row is not None else None

    def query(
        self,
        *,
        harness: Optional[str] = None,
        conversation_id: Optional[str] = None,
        session_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> list[SourceEvent]:
        clauses = []
        params: dict[str, Any] = {}
        if harness is not None:
            clauses.append("harness = :harness")
            params["harness"] = harness
        if conversation_id is not None:
            clauses.append("conversation_id = :conversation_id")
            params["conversation_id"] = conversation_id
        if session_id is not None:
            clauses.append("session_id = :session_id")
            params["session_id"] = session_id
        if since is not None:
            clauses.append("observed_at >= :since")
            params["since"] = since.isoformat()
        if until is not None:
            clauses.append("observed_at <= :until")
            params["until"] = until.isoformat()

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = self._conn.execute(f"SELECT * FROM events {where} ORDER BY observed_at ASC", params)
        return [_row_to_event(row) for row in cursor.fetchall()]

    def stats(self) -> dict[str, Any]:
        """Summary counts used by the journal CLI's `stats` subcommand."""
        total = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        by_harness = dict(self._conn.execute("SELECT harness, COUNT(*) FROM events GROUP BY harness").fetchall())
        by_type = dict(self._conn.execute("SELECT event_type, COUNT(*) FROM events GROUP BY event_type").fetchall())
        by_retention = dict(
            self._conn.execute("SELECT retention_class, COUNT(*) FROM events GROUP BY retention_class").fetchall()
        )
        date_range = self._conn.execute("SELECT MIN(observed_at), MAX(observed_at) FROM events").fetchone()
        # Live-captured vs. reconstructed/backfilled, per harness — surfaces
        # what fraction of a harness's total is genuine evidence journaled
        # directly from an export vs. a retrospective reconstruction (see
        # metadata.provenance_reconstructed in docs/schemas/source-event-1.0.json).
        # Not a distinct storage class from `raw` retention_class above; this
        # is about capture fidelity, not content redaction.
        provenance_rows = self._conn.execute(
            "SELECT harness, "
            "  SUM(CASE WHEN json_extract(metadata_json, '$.provenance_reconstructed') = 1 THEN 1 ELSE 0 END) AS reconstructed, "
            "  SUM(CASE WHEN json_extract(metadata_json, '$.provenance_reconstructed') IS NOT 1 THEN 1 ELSE 0 END) AS captured "
            "FROM events GROUP BY harness"
        ).fetchall()
        by_provenance = {
            row["harness"]: {"captured": row["captured"], "reconstructed": row["reconstructed"]} for row in provenance_rows
        }
        return {
            "total_events": total,
            "by_harness": by_harness,
            "by_event_type": by_type,
            "by_retention_class": by_retention,
            "by_provenance": by_provenance,
            "observed_at_range": {"min": date_range[0], "max": date_range[1]},
        }


def _event_to_row(event: SourceEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "schema_version": event.schema_version,
        "event_type": event.event_type,
        "harness": event.source.harness,
        "account_scope": event.source.account_scope,
        "conversation_id": event.source.conversation_id,
        "session_id": event.source.session_id,
        "turn_id": event.source.turn_id,
        "model": event.source.model,
        "actor_type": event.actor_type,
        "actor_id": event.actor_id,
        "observed_at": _iso(event.observed_at),
        "event_date": _iso(event.event_date) if event.event_date else None,
        "date_precision": event.date_precision.value,
        "content_json": json.dumps(event.content, sort_keys=True, ensure_ascii=False),
        "content_hash": event.content_hash,
        "parent_event_ids_json": json.dumps(event.parent_event_ids),
        "attachment_refs_json": json.dumps(event.attachment_refs),
        "privacy_json": json.dumps(event.privacy, sort_keys=True),
        "metadata_json": json.dumps(event.metadata, sort_keys=True, ensure_ascii=False),
        "retention_class": event.privacy.get("retention_class", "raw"),
        "inserted_at": datetime.now(timezone.utc).isoformat(),
    }


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _row_to_event(row: sqlite3.Row) -> SourceEvent:
    return SourceEvent(
        schema_version=row["schema_version"],
        event_id=row["event_id"],
        event_type=row["event_type"],
        source=SourceProvenance(
            harness=row["harness"],
            account_scope=row["account_scope"],
            conversation_id=row["conversation_id"],
            session_id=row["session_id"],
            turn_id=row["turn_id"],
            model=row["model"],
        ),
        observed_at=_parse_dt(row["observed_at"]),
        content=json.loads(row["content_json"]),
        content_hash=row["content_hash"],
        actor_type=row["actor_type"],
        actor_id=row["actor_id"],
        event_date=_parse_dt(row["event_date"]),
        date_precision=DatePrecision(row["date_precision"]),
        parent_event_ids=json.loads(row["parent_event_ids_json"]),
        attachment_refs=json.loads(row["attachment_refs_json"]),
        privacy=json.loads(row["privacy_json"]),
        metadata=json.loads(row["metadata_json"]),
    )
