"""Outcome of each background `remember` write: queued, committed or failed.

`remember` acks as soon as an episode is queued (remember_queued in
server/providers/memory_graphiti.py), and the real write runs afterwards. A
failure there used to reach only the server log, so a user and their AI read
the ack as success while the graph stayed empty (docs/setup-simplification-
plan.md, P7). This log records every outcome, with a plain-language reason for
failures, so the `memory_status` tool, `capture_health` and `/health` can show it.

Kept in the journal database beside the OAuth tables. A row still `queued`
from before this server process started can never finish, so it reads as
`lost`.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from server.journal.store import DEFAULT_JOURNAL_PATH

QUEUED = "queued"
COMMITTED = "committed"
FAILED = "failed"
LOST = "lost"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS background_writes (
    episode_name TEXT PRIMARY KEY,
    graph TEXT NOT NULL,
    status TEXT NOT NULL,
    error_kind TEXT,
    error_message TEXT,
    queued_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_background_writes_queued_at ON background_writes(queued_at);
"""

_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BackgroundWriteLog:
    def __init__(self, db_path: Optional[Path] = None, process_started_at: Optional[str] = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_JOURNAL_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._started_at = process_started_at or _PROCESS_STARTED_AT
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def queued(self, episode_name: str, graph: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO background_writes (episode_name, graph, status, queued_at) VALUES (?, ?, ?, ?)",
                (episode_name, graph, QUEUED, _now()),
            )
            self._conn.commit()

    def committed(self, episode_name: str) -> None:
        self._finish(episode_name, COMMITTED, None, None)

    def failed(self, episode_name: str, error_kind: str, error_message: str) -> None:
        self._finish(episode_name, FAILED, error_kind, error_message)

    def _finish(self, episode_name: str, status: str, kind: Optional[str], message: Optional[str]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE background_writes SET status = ?, error_kind = ?, error_message = ?, finished_at = ? "
                "WHERE episode_name = ?",
                (status, kind, message, _now(), episode_name),
            )
            self._conn.commit()

    def _row(self, row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        if out["status"] == QUEUED and out["queued_at"] < self._started_at:
            out["status"] = LOST
            out["error_message"] = "The server restarted before this write finished; nothing was saved."
        return out

    def get(self, episode_name: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM background_writes WHERE episode_name = ?", (episode_name,)
            ).fetchone()
        return self._row(row) if row is not None else None

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM background_writes ORDER BY queued_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row(r) for r in rows]


_DEFAULT_LOG: Optional[BackgroundWriteLog] = None


def get_default_write_log() -> BackgroundWriteLog:
    global _DEFAULT_LOG
    if _DEFAULT_LOG is None:
        _DEFAULT_LOG = BackgroundWriteLog()
    return _DEFAULT_LOG


def format_write(row: dict[str, Any]) -> str:
    line = f"- `{row['episode_name']}` ({row['graph']}): **{row['status']}**, queued {row['queued_at'][:19]}Z"
    if row.get("finished_at"):
        line += f", finished {row['finished_at'][:19]}Z"
    if row.get("error_message"):
        line += f"\n  {row['error_message']}"
    return line


def format_write_status(rows: list[dict[str, Any]], episode_name: Optional[str] = None) -> str:
    if episode_name and not rows:
        return (
            f"No background write named `{episode_name}` is recorded. Check the exact episode name from "
            "remember's reply, or call memory_status with no name to see the latest writes."
        )
    if not rows:
        return "No background memory writes recorded yet."
    header = "Background memory write:" if episode_name else f"Latest {len(rows)} background memory write(s):"
    lines = [header, *(format_write(r) for r in rows)]
    if any(r["status"] == QUEUED for r in rows):
        lines.append("_`queued` writes are still being processed; check again in a minute._")
    return "\n".join(lines)
