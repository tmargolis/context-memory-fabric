"""Byte-offset incremental tailing of Antigravity IDE transcript.jsonl files.

Antigravity writes a live-growing JSONL transcript per conversation at
`<app_data_dir>/brain/<conversation_id>/.system_generated/logs/transcript.jsonl`,
where `<app_data_dir>` is `~/.gemini/antigravity` (the active install) or
`~/.gemini/antigravity-ide` (a secondary/legacy install that also has real,
if sparser, recent data -- both are tailed by default, the user 2026-09-24).
This mirrors server.adapters.claude_code.transcript_reader's byte-offset
tailing (`antigravity_tail_state`, a new table in the same journal.db) --
see that module's docstring for the general rationale (avoid re-reading/
re-journaling already-seen lines; a trailing partial line is left for the
next pass rather than parsed against truncated JSON).

One deliberate divergence from claude_code's version: `read_new_lines` here
does NOT persist the tail offset itself. It yields (line, byte_offset_after)
pairs and leaves persistence to the caller (server.adapters.antigravity.worker),
because worker.process_pending's `until` cutoff (see its docstring) needs to
stop short of end-of-file and persist only the offset of the last line it
actually kept -- so a later, wider backfill can still see everything after
the cutoff. claude_code never needed this (its tailing is always open-ended
in production), so its reader always advances straight to EOF.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterator, Optional

from server.journal.store import DEFAULT_JOURNAL_PATH

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS antigravity_tail_state (
    file_path TEXT PRIMARY KEY,
    conversation_id TEXT,
    app_data_dir TEXT,
    byte_offset INTEGER NOT NULL DEFAULT 0,
    last_line_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""

DEFAULT_APP_DATA_DIRS = [
    Path.home() / ".gemini" / "antigravity",
    Path.home() / ".gemini" / "antigravity-ide",
]


@dataclass
class TranscriptFile:
    path: Path
    conversation_id: str
    app_data_dir: Path  # root dir, e.g. ~/.gemini/antigravity -- .name gives the short label


class TailStateStore:
    """Owns the `antigravity_tail_state` table. Same db file as the journal
    (server.journal.store.DEFAULT_JOURNAL_PATH by default), a distinct
    table -- events stays append-only and untouched by this module.
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
            "SELECT byte_offset FROM antigravity_tail_state WHERE file_path = ?",
            (str(file_path),),
        ).fetchone()
        return row["byte_offset"] if row is not None else 0

    def set_offset(
        self,
        file_path: Path,
        *,
        conversation_id: str,
        app_data_dir: str,
        byte_offset: int,
        line_count_delta: int,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO antigravity_tail_state (file_path, conversation_id, app_data_dir, byte_offset, last_line_count, updated_at)
            VALUES (:file_path, :conversation_id, :app_data_dir, :byte_offset, :line_count_delta, :updated_at)
            ON CONFLICT(file_path) DO UPDATE SET
                byte_offset = :byte_offset,
                last_line_count = last_line_count + :line_count_delta,
                updated_at = :updated_at
            """,
            {
                "file_path": str(file_path),
                "conversation_id": conversation_id,
                "app_data_dir": app_data_dir,
                "byte_offset": byte_offset,
                "line_count_delta": line_count_delta,
                "updated_at": now,
            },
        )
        self._conn.commit()

    def all_known_files(self) -> list[str]:
        rows = self._conn.execute("SELECT file_path FROM antigravity_tail_state").fetchall()
        return [r["file_path"] for r in rows]


def discover_transcript_files(app_data_dirs: Optional[list[Path]] = None) -> list[TranscriptFile]:
    """Find every `brain/<conversation_id>/.system_generated/logs/transcript.jsonl`
    under each app_data_dir (default: both known Antigravity install roots).
    """
    roots = app_data_dirs if app_data_dirs is not None else DEFAULT_APP_DATA_DIRS
    out: list[TranscriptFile] = []
    for root in roots:
        brain_dir = root / "brain"
        if not brain_dir.is_dir():
            continue
        for conv_dir in sorted(p for p in brain_dir.iterdir() if p.is_dir()):
            transcript_path = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
            if not transcript_path.is_file():
                continue
            out.append(
                TranscriptFile(
                    path=transcript_path,
                    conversation_id=conv_dir.name,
                    app_data_dir=root,
                )
            )
    return out


def read_new_lines(
    file_path: Path,
    tail_store: TailStateStore,
) -> Iterator[tuple[str, int]]:
    """Yield (line, byte_offset_after_this_line) for every complete new line
    since the stored offset. Does NOT persist the offset -- see module
    docstring; the caller decides what to persist (full EOF, or a cutoff
    short of it). A trailing partial line (file being written mid-poll) is
    left unconsumed and simply reappears on the next pass.
    """
    start_offset = tail_store.get_offset(file_path)
    file_size = file_path.stat().st_size
    if file_size < start_offset:
        # File was truncated/replaced -- restart from 0 rather than seek
        # past EOF.
        start_offset = 0

    new_offset = start_offset
    with file_path.open("rb") as f:
        f.seek(start_offset)
        buffer = b""
        while True:
            chunk = f.read(1 << 20)
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
