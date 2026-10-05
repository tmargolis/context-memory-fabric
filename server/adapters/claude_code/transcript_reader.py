"""Byte-offset incremental tailing of Claude Code / Code-tab transcript files.

Transcript files grow live during a session (the CLI/Desktop append a new
JSONL line per turn as the session proceeds), so a full-file re-read on
every poll would be wasteful and would re-journal already-seen lines
through parser.parse_line's idempotent-by-event_id path -- harmless, but
needless I/O and CPU at scale. This module tracks a byte offset per file in
`claude_code_tail_state`, a new table living in the same journal.db SQLite
file as the event journal (same one-file-per-concern pattern as
server.consolidation.threads.ThreadIndex).

Partial-line handling: a transcript file can be read mid-append (the writer
process is not coordinating with us). `read_new_lines` only advances the
stored offset past the last **complete** line (terminated by `\n`) it
successfully read -- a trailing partial line is left for the next tail
pass rather than parsed against truncated JSON.

Project scoping: CMF_CLAUDE_CODE_PROJECT_ALLOW / _DENY (comma-separated
project-slug or path substrings) let an operator scope which projects
under ~/.claude/projects/ get tailed at all -- ALLOW wins if both are set
(an explicit allowlist is a stronger statement than a denylist against the
same run). Neither set means "tail everything found" -- except the
ephemeral temp-dir projects below.

Temp-dir projects are skipped by default (2026-10-02,
docs/CLAUDE-HARNESS-PROVENANCE-PLAN.md): `claude -p` runs spawned with a
temp-dir cwd -- the MS7/MS9 answer-eval generator, scratchpad sessions --
land under `~/.claude/projects/-private-var-folders-...` and were 1,330 of
the 1,413 journaled `claude_code` conversations. None is a real working
session. Set CMF_CLAUDE_CODE_INCLUDE_TEMP=1 to tail them anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from typing import Iterator, Optional

from server.journal.store import DEFAULT_JOURNAL_PATH

TABLE = "claude_code_tail_state"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS claude_code_tail_state (
    file_path TEXT PRIMARY KEY,
    project_slug TEXT,
    session_id TEXT,
    byte_offset INTEGER NOT NULL DEFAULT 0,
    last_line_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""

DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"


@dataclass
class TranscriptFile:
    path: Path
    project_slug: str
    session_id: str


class TailStateStore:
    """Owns the `claude_code_tail_state` table. Same db file as the journal
    (server.journal.store.DEFAULT_JOURNAL_PATH by default), a distinct
    table -- events stays append-only and untouched by this module.
    """

    def __init__(self, db_path: Optional[Path] = None, table: str = TABLE) -> None:
        # `table` lets another adapter on the same JSONL format keep its own
        # offsets (claude_cowork_tail_state). Identifier, so whitelisted.
        if not table.replace("_", "").isalnum():
            raise ValueError(f"bad tail-state table name {table!r}")
        self.table = table
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_JOURNAL_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA_SQL.replace(TABLE, table))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "TailStateStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get_offset(self, file_path: Path) -> int:
        row = self._conn.execute(
            f"SELECT byte_offset FROM {self.table} WHERE file_path = ?",
            (str(file_path),),
        ).fetchone()
        return row["byte_offset"] if row is not None else 0

    def set_offset(self, file_path: Path, *, project_slug: str, session_id: str, byte_offset: int, line_count_delta: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            f"""
            INSERT INTO {self.table} (file_path, project_slug, session_id, byte_offset, last_line_count, updated_at)
            VALUES (:file_path, :project_slug, :session_id, :byte_offset, :line_count_delta, :updated_at)
            ON CONFLICT(file_path) DO UPDATE SET
                byte_offset = :byte_offset,
                last_line_count = last_line_count + :line_count_delta,
                updated_at = :updated_at
            """,
            {
                "file_path": str(file_path),
                "project_slug": project_slug,
                "session_id": session_id,
                "byte_offset": byte_offset,
                "line_count_delta": line_count_delta,
                "updated_at": now,
            },
        )
        self._conn.commit()

    def all_known_files(self) -> list[str]:
        rows = self._conn.execute(f"SELECT file_path FROM {self.table}").fetchall()
        return [r["file_path"] for r in rows]


# Project-slug prefixes of temp-dir cwds (macOS $TMPDIR is under
# /private/var/folders, /tmp resolves to /private/tmp).
EPHEMERAL_SLUG_PREFIXES = ("-private-var-folders-", "-var-folders-", "-private-tmp-", "-tmp-")


def is_ephemeral_project(project_slug: str) -> bool:
    return project_slug.startswith(EPHEMERAL_SLUG_PREFIXES)


def _include_ephemeral() -> bool:
    return (os.getenv("CMF_CLAUDE_CODE_INCLUDE_TEMP") or "").strip().lower() in ("1", "true", "yes")


def _project_filters() -> tuple[set[str], set[str]]:
    allow = {s.strip() for s in (os.getenv("CMF_CLAUDE_CODE_PROJECT_ALLOW") or "").split(",") if s.strip()}
    deny = {s.strip() for s in (os.getenv("CMF_CLAUDE_CODE_PROJECT_DENY") or "").split(",") if s.strip()}
    return allow, deny


def _project_allowed(project_slug: str, allow: set[str], deny: set[str]) -> bool:
    if allow:
        return any(a in project_slug for a in allow)
    if deny:
        return not any(d in project_slug for d in deny)
    return True


def discover_transcript_files(projects_root: Optional[Path] = None) -> list[TranscriptFile]:
    """Find every `<session-uuid>.jsonl` under `~/.claude/projects/*/`,
    minus temp-dir projects (unless CMF_CLAUDE_CODE_INCLUDE_TEMP), filtered
    by CMF_CLAUDE_CODE_PROJECT_ALLOW/_DENY.
    """
    root = projects_root or DEFAULT_PROJECTS_ROOT
    if not root.is_dir():
        return []

    allow, deny = _project_filters()
    include_ephemeral = _include_ephemeral()
    out: list[TranscriptFile] = []
    for project_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        slug = project_dir.name
        if is_ephemeral_project(slug) and not include_ephemeral:
            continue
        if not _project_allowed(slug, allow, deny):
            continue
        for jsonl_path in sorted(project_dir.glob("*.jsonl")):
            session_id = jsonl_path.stem
            out.append(TranscriptFile(path=jsonl_path, project_slug=slug, session_id=session_id))
    return out


def read_new_lines(file_path: Path, tail_store: TailStateStore, *, project_slug: str, session_id: str) -> Iterator[str]:
    """Yield every complete new line since the stored offset, then advance
    the stored offset to just past the last complete line read. A trailing
    partial line (file being written mid-poll) is left unconsumed.
    """
    start_offset = tail_store.get_offset(file_path)
    file_size = file_path.stat().st_size
    if file_size < start_offset:
        # File was truncated/replaced (e.g. a new session reused a path in
        # a test fixture) -- restart from 0 rather than seek past EOF.
        start_offset = 0

    lines_yielded = 0
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
                lines_yielded += 1
                yield raw.decode("utf-8", errors="replace")

    tail_store.set_offset(
        file_path,
        project_slug=project_slug,
        session_id=session_id,
        byte_offset=new_offset,
        line_count_delta=lines_yielded,
    )
