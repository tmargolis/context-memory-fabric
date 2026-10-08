"""Byte-offset incremental tailing of Codex transcript files (MS4d).

Reads the JSONL transcripts Codex writes per session at
`~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<timestamp>-<uuid>.jsonl`.

Follows Antigravity's caller-managed offset pattern:
1. `read_new_lines` reads complete lines from a stored byte offset, detecting
   file replacement/truncation and deferring trailing partial lines.
2. `TailStateStore` tracks byte offsets in `codex_tail_state` and adapter metadata
   in `codex_adapter_meta` within the existing journal SQLite database.
3. Offsets advance ONLY after successful writes to the journal store.
4. Process-level `WorkerLock` (server.adapters.file_lock, portable) prevents hook/poller concurrency.
5. Initial capture cutoff is persisted on first run; older history requires
   explicit backfill mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator, Optional

from server.adapters.codex.parser import (
    ParseStats,
    extract_session_meta,
    parse_line,
)
from server.adapters.codex.project import resolve_project
from server.adapters.file_lock import WorkerLock as _FileLock
from server.core.models import SourceEvent
from server.journal.store import DEFAULT_JOURNAL_PATH, SqliteEventStore

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS codex_tail_state (
    file_path TEXT PRIMARY KEY,
    session_id TEXT,
    conversation_id TEXT,
    project_slug TEXT,
    byte_offset INTEGER NOT NULL DEFAULT 0,
    last_line_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS codex_adapter_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS codex_pending_extractions (
    conversation_id TEXT PRIMARY KEY,
    session_id TEXT,
    project_slug TEXT,
    queued_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL
);
"""

META_KEY_INITIAL_CUTOFF = "initial_capture_cutoff"

_FILENAME_UUID_RE = re.compile(
    r"rollout-(?:\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-)?([0-9a-fA-F-]+)\.jsonl$"
)


def get_default_sessions_root() -> Path:
    """CMF_CODEX_SESSIONS_ROOT, else `$CODEX_HOME/sessions` (Codex's own
    setting for where it keeps its state), else `~/.codex/sessions`."""
    env_root = os.environ.get("CMF_CODEX_SESSIONS_ROOT")
    if env_root:
        return Path(env_root).expanduser()
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "sessions"
    return Path.home() / ".codex" / "sessions"


@dataclass
class TranscriptFile:
    path: Path
    session_id: str
    date_str: str
    mtime: float
    size_bytes: int


@dataclass
class CaptureResult:
    files_scanned: int = 0
    files_with_new_bytes: int = 0
    lines_read: int = 0
    events_captured: int = 0
    events_deduped: int = 0
    events_skipped: int = 0
    failed_records: int = 0
    conversations_touched: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class TailStateStore:
    """Owns the `codex_tail_state` and `codex_adapter_meta` tables in journal.db.

    Additive and versioned; does not mutate events or schema.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_JOURNAL_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "TailStateStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get_offset(self, file_path: Path) -> int:
        row = self._conn.execute(
            "SELECT byte_offset FROM codex_tail_state WHERE file_path = ?",
            (str(file_path),),
        ).fetchone()
        return row["byte_offset"] if row is not None else 0

    def get_tail_state(self, file_path: Path) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM codex_tail_state WHERE file_path = ?",
            (str(file_path),),
        ).fetchone()
        return dict(row) if row is not None else None

    def set_offset(
        self,
        file_path: Path,
        *,
        session_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        project_slug: Optional[str] = None,
        byte_offset: int,
        line_count_delta: int = 0,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO codex_tail_state (
                file_path, session_id, conversation_id, project_slug,
                byte_offset, last_line_count, updated_at
            ) VALUES (
                :file_path, :session_id, :conversation_id, :project_slug,
                :byte_offset, :line_count_delta, :updated_at
            )
            ON CONFLICT(file_path) DO UPDATE SET
                session_id = COALESCE(:session_id, session_id),
                conversation_id = COALESCE(:conversation_id, conversation_id),
                project_slug = COALESCE(:project_slug, project_slug),
                byte_offset = :byte_offset,
                last_line_count = last_line_count + :line_count_delta,
                updated_at = :updated_at
            """,
            {
                "file_path": str(file_path),
                "session_id": session_id,
                "conversation_id": conversation_id,
                "project_slug": project_slug,
                "byte_offset": byte_offset,
                "line_count_delta": line_count_delta,
                "updated_at": now,
            },
        )
        self._conn.commit()

    def all_known_files(self) -> list[str]:
        rows = self._conn.execute("SELECT file_path FROM codex_tail_state").fetchall()
        return [r["file_path"] for r in rows]

    def get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM codex_adapter_meta WHERE key = ?",
            (key,),
        ).fetchone()
        return row["value"] if row is not None else None

    def set_meta(self, key: str, value: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO codex_adapter_meta (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now),
        )
        self._conn.commit()

    def get_initial_cutoff(self) -> Optional[datetime]:
        val = self.get_meta(META_KEY_INITIAL_CUTOFF)
        if not val:
            return None
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            return None

    def set_initial_cutoff(self, cutoff: datetime) -> None:
        self.set_meta(META_KEY_INITIAL_CUTOFF, cutoff.isoformat())

    def get_or_create_initial_cutoff(self, default_now: Optional[datetime] = None) -> datetime:
        existing = self.get_initial_cutoff()
        if existing is not None:
            return existing
        now = default_now or datetime.now(timezone.utc)
        self.set_initial_cutoff(now)
        return now

    def enqueue_extraction(
        self,
        conversation_id: str,
        *,
        session_id: Optional[str] = None,
        project_slug: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO codex_pending_extractions (
                conversation_id, session_id, project_slug, queued_at, attempts, updated_at
            ) VALUES (
                :conversation_id, :session_id, :project_slug, :queued_at, 0, :updated_at
            )
            ON CONFLICT(conversation_id) DO UPDATE SET
                session_id = COALESCE(:session_id, session_id),
                project_slug = COALESCE(:project_slug, project_slug),
                updated_at = :updated_at
            """,
            {
                "conversation_id": conversation_id,
                "session_id": session_id,
                "project_slug": project_slug,
                "queued_at": now,
                "updated_at": now,
            },
        )
        self._conn.commit()

    def get_pending_extractions(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM codex_pending_extractions ORDER BY queued_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def complete_extraction(self, conversation_id: str) -> None:
        self._conn.execute(
            "DELETE FROM codex_pending_extractions WHERE conversation_id = ?",
            (conversation_id,),
        )
        self._conn.commit()

    def record_extraction_failure(self, conversation_id: str, error: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE codex_pending_extractions
            SET attempts = attempts + 1, last_error = ?, updated_at = ?
            WHERE conversation_id = ?
            """,
            (error, now, conversation_id),
        )
        self._conn.commit()


class WorkerLock(_FileLock):
    """Non-blocking process-level file lock (server.adapters.file_lock),
    defaulting to the Codex worker's own lock file.

    Prevents concurrent execution between scheduled poller runs and hook invocations.
    """

    def __init__(self, lock_path: Optional[Path] = None) -> None:
        super().__init__(
            Path(lock_path) if lock_path is not None else DEFAULT_JOURNAL_PATH.parent / "codex_worker.lock"
        )


def read_new_lines(
    file_path: Path,
    start_offset: int,
) -> Iterator[tuple[str, int]]:
    """Yield (line_str, new_byte_offset) for every complete line since start_offset.

    Does NOT persist offsets. If file was truncated or replaced, restarts from 0.
    A trailing partial line is left unconsumed in the buffer.
    """
    if not file_path.is_file():
        return

    try:
        file_size = file_path.stat().st_size
    except OSError:
        return

    if file_size < start_offset:
        logger.warning(
            "codex transcript file %s truncated/replaced (size %d < offset %d), rewinding to 0",
            file_path,
            file_size,
            start_offset,
        )
        start_offset = 0

    new_offset = start_offset
    with file_path.open("rb") as f:
        f.seek(start_offset)
        buffer = b""
        while True:
            chunk = f.read(1 << 20)  # 1MB buffer
            if not chunk:
                break
            buffer += chunk
            while True:
                idx = buffer.find(b"\n")
                if idx == -1:
                    break
                raw = buffer[: idx + 1]
                buffer = buffer[idx + 1 :]
                new_offset += len(raw)
                yield raw.decode("utf-8", errors="replace"), new_offset


def _extract_uuid_from_filename(filename: str) -> str:
    m = _FILENAME_UUID_RE.search(filename)
    if m:
        return m.group(1)
    return Path(filename).stem


def discover_transcript_files(
    sessions_root: Optional[Path] = None,
    *,
    explicit_path: Optional[Path] = None,
    session_id_filter: Optional[str] = None,
    project_filter: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> list[TranscriptFile]:
    """Discover Codex session transcript files.

    If explicit_path is provided, scopes exclusively to that file.
    Otherwise scans sessions_root for `**/*.jsonl`.
    """
    root = sessions_root if sessions_root is not None else get_default_sessions_root()

    if explicit_path is not None:
        p = Path(explicit_path).expanduser().resolve()
        if not p.is_file():
            return []
        try:
            st = p.stat()
        except OSError:
            return []
        uuid_str = _extract_uuid_from_filename(p.name)
        date_str = p.parent.name if len(p.parent.name) == 2 else "unknown"
        return [
            TranscriptFile(
                path=p,
                session_id=uuid_str,
                date_str=date_str,
                mtime=st.st_mtime,
                size_bytes=st.st_size,
            )
        ]

    if not root.is_dir():
        return []

    discovered: list[TranscriptFile] = []
    for path in root.glob("**/*.jsonl"):
        if not path.is_file():
            continue
        try:
            st = path.stat()
        except OSError:
            continue

        if since is not None and st.st_mtime < since.timestamp():
            continue
        if until is not None and st.st_mtime > until.timestamp():
            continue

        uuid_str = _extract_uuid_from_filename(path.name)
        if session_id_filter and (
            session_id_filter != uuid_str and not uuid_str.startswith(session_id_filter)
        ):
            continue

        # Extract date from directory structure .../<YYYY>/<MM>/<DD>/rollout-...
        parts = path.parts
        date_str = (
            f"{parts[-4]}-{parts[-3]}-{parts[-2]}"
            if len(parts) >= 4 and parts[-4].isdigit() and len(parts[-4]) == 4
            else "unknown"
        )

        discovered.append(
            TranscriptFile(
                path=path,
                session_id=uuid_str,
                date_str=date_str,
                mtime=st.st_mtime,
                size_bytes=st.st_size,
            )
        )

    # Sort oldest-first for monotonic chronological processing
    discovered.sort(key=lambda tf: (tf.mtime, str(tf.path)))

    if limit is not None and limit > 0:
        discovered = discovered[:limit]

    return discovered


def preview_transcript_file(
    file_path: Path,
    tail_store: TailStateStore,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> dict[str, Any]:
    """True read-only preview of what would be captured from file_path without writes."""
    start_offset = tail_store.get_offset(file_path)
    stats = ParseStats()
    events_preview = []
    bytes_pending = 0
    final_offset = start_offset

    conversation_id = _extract_uuid_from_filename(file_path.name)
    session_id = conversation_id
    project: Optional[str] = None
    parent_conversation_id: Optional[str] = None

    for line, new_offset in read_new_lines(file_path, start_offset):
        bytes_pending += len(line.encode("utf-8"))
        final_offset = new_offset

        meta = extract_session_meta(line)
        if meta:
            if meta.get("conversation_id"):
                conversation_id = meta["conversation_id"]
            if meta.get("session_id"):
                session_id = meta["session_id"]
            if meta.get("parent_thread_id"):
                parent_conversation_id = meta["parent_thread_id"]
            if meta.get("cwd"):
                project = resolve_project(meta.get("cwd"), meta.get("workspace_roots"))

        ev = parse_line(
            line,
            conversation_id=conversation_id,
            session_id=session_id,
            parent_conversation_id=parent_conversation_id,
            project=project,
            stats=stats,
        )
        if ev:
            if since and ev.observed_at < since:
                continue
            if until and ev.observed_at > until:
                continue
            events_preview.append(
                {
                    "event_id": ev.event_id,
                    "actor_type": ev.actor_type,
                    "kind": ev.metadata.get("content_kind"),
                    "observed_at": ev.observed_at.isoformat(),
                    "project": project,
                }
            )

    return {
        "file_path": str(file_path),
        "start_offset": start_offset,
        "final_offset": final_offset,
        "bytes_pending": bytes_pending,
        "lines_seen": stats["lines_seen"],
        "kept_events_count": len(events_preview),
        "skipped_known": stats["skipped_known"],
        "skipped_unknown": stats["skipped_unknown"],
        "lines_unparseable": stats["lines_unparseable"],
        "conversation_id": conversation_id,
        "project": project,
        "sample_events": events_preview[:5],
    }


def capture_file_to_journal(
    file_path: Path,
    journal_store: SqliteEventStore,
    tail_store: TailStateStore,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    dry_run: bool = False,
) -> CaptureResult:
    """Incremental evidence capture for one transcript file.

    Persists offsets ONLY after successful event insertion.
    Returns detailed counts of captured, deduped, and skipped records.
    """
    res = CaptureResult(files_scanned=1)
    start_offset = tail_store.get_offset(file_path)

    conversation_id = _extract_uuid_from_filename(file_path.name)
    session_id = conversation_id
    project: Optional[str] = None
    parent_conversation_id: Optional[str] = None

    stats = ParseStats()
    persisted_offset = start_offset
    line_count = 0

    for line, new_offset in read_new_lines(file_path, start_offset):
        line_count += 1
        meta = extract_session_meta(line)
        if meta:
            if meta.get("conversation_id"):
                conversation_id = meta["conversation_id"]
            if meta.get("session_id"):
                session_id = meta["session_id"]
            if meta.get("parent_thread_id"):
                parent_conversation_id = meta["parent_thread_id"]
            if meta.get("cwd"):
                project = resolve_project(meta.get("cwd"), meta.get("workspace_roots"))

        ev = parse_line(
            line,
            conversation_id=conversation_id,
            session_id=session_id,
            parent_conversation_id=parent_conversation_id,
            project=project,
            stats=stats,
        )

        if ev is not None:
            if since is not None and ev.observed_at < since:
                res.events_skipped += 1
            elif until is not None and ev.observed_at > until:
                res.events_skipped += 1
                # If we passed until cutoff, don't advance persisted_offset further
                break
            else:
                if not dry_run:
                    try:
                        inserted = journal_store.append(ev)
                        if inserted:
                            res.events_captured += 1
                        else:
                            res.events_deduped += 1
                    except Exception as ex:
                        res.failed_records += 1
                        res.errors.append(f"Failed to journal event {ev.event_id}: {ex}")
                        logger.exception("Error journaling event %s", ev.event_id)
                        # Stop advancing offset past the failed event
                        break
                else:
                    res.events_captured += 1

        persisted_offset = new_offset

    res.lines_read = line_count
    if line_count > 0:
        res.files_with_new_bytes = 1
        res.conversations_touched.append(conversation_id)

    if not dry_run and persisted_offset != start_offset:
        tail_store.set_offset(
            file_path,
            session_id=session_id,
            conversation_id=conversation_id,
            project_slug=project,
            byte_offset=persisted_offset,
            line_count_delta=line_count,
        )

    return res
