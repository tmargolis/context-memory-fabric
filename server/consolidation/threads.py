"""Cross-harness, cross-session reasoning-thread index (ADR 0004 decision 3,
carried forward by ADR 0005 decision 4).

A small persistent structure of open threads of thinking. Its whole reason
to exist: Todd routinely starts a line of reasoning in one assistant (say
Claude) and continues it in another (Gemini), or opens a fresh
session/thread with the same assistant to keep that provider's context
small. So a thread here is keyed on a **normalized topic/project key that
ignores which harness and which conversation** — an intent stated in a
Claude thread on Tuesday and its resolution in a Gemini thread on Friday
land on the same `thread_key`.

Lives in the same SQLite file as the journal / consolidation store — a
thread only ever references events by `event_id`, so one file keeps that a
trivial fact rather than a cross-database join.

Matching is deliberately simple for now: exact normalized-key equality.
ADR 0005 flags fuzzy/embedding thread-matching as later work; the model
supplies the `thread_key` and `normalize_key` collapses trivial variants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Optional

from server.journal.store import DEFAULT_JOURNAL_PATH

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reasoning_threads (
    thread_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    first_seen_event_id TEXT,
    first_seen_at TEXT,
    last_seen_event_id TEXT,
    last_seen_at TEXT,
    harnesses_json TEXT NOT NULL DEFAULT '[]',
    conversation_ids_json TEXT NOT NULL DEFAULT '[]',
    event_ids_json TEXT NOT NULL DEFAULT '[]',
    reasoning_kinds_json TEXT NOT NULL DEFAULT '[]',
    episode_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reasoning_threads_status ON reasoning_threads(status);
CREATE INDEX IF NOT EXISTS idx_reasoning_threads_last_seen ON reasoning_threads(last_seen_at);
"""

_OPEN = "open"
_RESOLVED = "resolved"


def normalize_key(raw: str) -> str:
    """Collapse a free-text topic/project label to a stable key.

    "OpenClaw gateway connection issue" / "openclaw-gateway-connection" /
    "  OpenClaw   Gateway  Connection  " all map to
    "openclaw-gateway-connection".
    """
    s = (raw or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "untitled"


@dataclass
class ThreadRecord:
    thread_key: str
    title: str
    status: str
    first_seen_event_id: Optional[str]
    first_seen_at: Optional[str]
    last_seen_event_id: Optional[str]
    last_seen_at: Optional[str]
    harnesses: list[str] = field(default_factory=list)
    conversation_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    reasoning_kinds: list[str] = field(default_factory=list)
    episode_count: int = 0

    @property
    def is_cross_harness(self) -> bool:
        return len(self.harnesses) > 1

    @property
    def is_cross_conversation(self) -> bool:
        return len(self.conversation_ids) > 1

    def to_context_dict(self) -> dict[str, Any]:
        """Compact form handed to the model in PolicyContext.open_threads so
        it can decide 'does this window continue an existing thread'.
        """
        return {
            "thread_key": self.thread_key,
            "title": self.title,
            "status": self.status,
            "harnesses": self.harnesses,
            "reasoning_kinds": self.reasoning_kinds,
            "last_seen_at": self.last_seen_at,
        }


def _row_to_record(row: sqlite3.Row) -> ThreadRecord:
    return ThreadRecord(
        thread_key=row["thread_key"],
        title=row["title"],
        status=row["status"],
        first_seen_event_id=row["first_seen_event_id"],
        first_seen_at=row["first_seen_at"],
        last_seen_event_id=row["last_seen_event_id"],
        last_seen_at=row["last_seen_at"],
        harnesses=json.loads(row["harnesses_json"]),
        conversation_ids=json.loads(row["conversation_ids_json"]),
        event_ids=json.loads(row["event_ids_json"]),
        reasoning_kinds=json.loads(row["reasoning_kinds_json"]),
        episode_count=row["episode_count"],
    )


class ThreadIndex:
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

    def __enter__(self) -> "ThreadIndex":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(self, thread_key: str) -> Optional[ThreadRecord]:
        row = self._conn.execute(
            "SELECT * FROM reasoning_threads WHERE thread_key = ?", (normalize_key(thread_key),)
        ).fetchone()
        return _row_to_record(row) if row else None

    def open_threads(self, limit: int = 50) -> list[ThreadRecord]:
        """Most-recently-active open threads, for PolicyContext.open_threads."""
        rows = self._conn.execute(
            "SELECT * FROM reasoning_threads WHERE status = ? ORDER BY last_seen_at DESC LIMIT ?",
            (_OPEN, limit),
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def record_episode(
        self,
        *,
        thread_key: str,
        title: str,
        reasoning_kind: str,
        event_ids: list[str],
        harness: str,
        conversation_id: Optional[str],
        observed_at: Optional[datetime] = None,
        status: Optional[str] = None,
    ) -> ThreadRecord:
        """Upsert a thread with one more episode's worth of links.

        Creates the thread on first sight; otherwise merges in the new
        harness / conversation / event ids / reasoning_kind and bumps
        last-seen. `status` is only written when the caller passes it (e.g.
        the model reports the thread resolved).
        """
        key = normalize_key(thread_key)
        now = datetime.now(timezone.utc).isoformat()
        seen_at = (observed_at.isoformat() if observed_at else now)
        existing = self._conn.execute(
            "SELECT * FROM reasoning_threads WHERE thread_key = ?", (key,)
        ).fetchone()

        if existing is None:
            self._conn.execute(
                """
                INSERT INTO reasoning_threads (
                    thread_key, title, status, first_seen_event_id, first_seen_at,
                    last_seen_event_id, last_seen_at, harnesses_json,
                    conversation_ids_json, event_ids_json, reasoning_kinds_json,
                    episode_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    title or key,
                    status or _OPEN,
                    event_ids[0] if event_ids else None,
                    seen_at,
                    event_ids[-1] if event_ids else None,
                    seen_at,
                    json.dumps([harness] if harness else []),
                    json.dumps([conversation_id] if conversation_id else []),
                    json.dumps(list(event_ids)),
                    json.dumps([reasoning_kind] if reasoning_kind else []),
                    1,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            return self.get(key)  # type: ignore[return-value]

        rec = _row_to_record(existing)
        harnesses = _merged(rec.harnesses, [harness] if harness else [])
        conversations = _merged(rec.conversation_ids, [conversation_id] if conversation_id else [])
        events = _merged(rec.event_ids, event_ids)
        kinds = _merged(rec.reasoning_kinds, [reasoning_kind] if reasoning_kind else [])
        # last-seen only moves forward
        last_at = max(filter(None, [rec.last_seen_at, seen_at])) if (rec.last_seen_at or seen_at) else None
        last_event = event_ids[-1] if (event_ids and seen_at >= (rec.last_seen_at or "")) else rec.last_seen_event_id

        self._conn.execute(
            """
            UPDATE reasoning_threads SET
                title = ?, status = ?, last_seen_event_id = ?, last_seen_at = ?,
                harnesses_json = ?, conversation_ids_json = ?, event_ids_json = ?,
                reasoning_kinds_json = ?, episode_count = ?, updated_at = ?
            WHERE thread_key = ?
            """,
            (
                rec.title or title or key,
                status or rec.status,
                last_event,
                last_at,
                json.dumps(harnesses),
                json.dumps(conversations),
                json.dumps(events),
                json.dumps(kinds),
                rec.episode_count + 1,
                now,
                key,
            ),
        )
        self._conn.commit()
        return self.get(key)  # type: ignore[return-value]

    def set_status(self, thread_key: str, status: str) -> None:
        self._conn.execute(
            "UPDATE reasoning_threads SET status = ?, updated_at = ? WHERE thread_key = ?",
            (status, datetime.now(timezone.utc).isoformat(), normalize_key(thread_key)),
        )
        self._conn.commit()

    def stats(self) -> dict[str, Any]:
        total = self._conn.execute("SELECT COUNT(*) FROM reasoning_threads").fetchone()[0]
        by_status = dict(
            self._conn.execute("SELECT status, COUNT(*) FROM reasoning_threads GROUP BY status").fetchall()
        )
        cross_harness = self._conn.execute(
            "SELECT COUNT(*) FROM reasoning_threads WHERE json_array_length(harnesses_json) > 1"
        ).fetchone()[0]
        cross_conv = self._conn.execute(
            "SELECT COUNT(*) FROM reasoning_threads WHERE json_array_length(conversation_ids_json) > 1"
        ).fetchone()[0]
        return {
            "total_threads": total,
            "by_status": by_status,
            "cross_harness_threads": cross_harness,
            "cross_conversation_threads": cross_conv,
        }


def _merged(existing: list[str], new: list[str]) -> list[str]:
    """Order-preserving union."""
    out = list(existing)
    seen = set(existing)
    for item in new:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out
