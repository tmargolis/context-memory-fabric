"""Discard everything extraction wrote for Claude Code subagent conversations,
so they can be re-extracted (MS10a task 3, 2026-10-08).

The first backfill of subagent transcripts ran before extraction knew that a
subagent's `user` turns are its parent agent's instructions, and attributed
them to the user. None of its output was reviewed or promoted, so it is
removed rather than superseded, and the conversations are re-extracted under
the corrected prompt. The journal's events are evidence and stay untouched.

Removed, for conversations whose id contains `:agent-`:
- consolidation_jobs, derived_memories, and their reviews / review_audit rows
  (the pipeline's own thread-merge rejections);
- episode-proposals mirror files and doc-proposals naming such a conversation;
- reasoning_threads: deleted when only subagents fed them, otherwise the
  subagent conversation/event ids are stripped and the episode count reduced.

Refuses to run if any of those episodes was reviewed by a person or promoted.
Dry run by default; `--apply` backs up journal.db and both proposal folders
first.

    uv run python scripts/discard_subagent_extraction.py
    uv run python scripts/discard_subagent_extraction.py --apply
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
import sys

from server.journal.store import DEFAULT_JOURNAL_PATH

ROOT = Path(__file__).resolve().parent.parent
MARK = ":agent-"
PIPELINE_REVIEWERS = {"pipeline-thread-merge", "pipeline"}


def _mirror_files() -> list[Path]:
    out = []
    for folder in (ROOT / "episode-proposals", ROOT / "doc-proposals"):
        if not folder.is_dir():
            continue
        for path in folder.rglob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            conv = data.get("conversation_id") or data.get("source_conversation_id") or ""
            if isinstance(conv, str) and MARK in conv:
                out.append(path)
    return sorted(out)


def plan(conn: sqlite3.Connection) -> dict:
    like = f"%{MARK}%"
    memories = [r[0] for r in conn.execute(
        "SELECT memory_id FROM derived_memories WHERE memory_id LIKE ? OR source_event_id LIKE ?", (like, like))]
    reviews = conn.execute(
        f"SELECT memory_id, reviewer FROM reviews WHERE memory_id IN ({','.join('?' * len(memories))})", memories
    ).fetchall() if memories else []
    human = [m for m, reviewer in reviews if reviewer not in PIPELINE_REVIEWERS]
    promoted = conn.execute(
        f"SELECT COUNT(*) FROM promotions WHERE memory_id IN ({','.join('?' * len(memories))})", memories
    ).fetchone()[0] if memories else 0
    threads = conn.execute(
        "SELECT thread_key, conversation_ids_json FROM reasoning_threads WHERE conversation_ids_json LIKE ?", (like,)
    ).fetchall()
    return {
        "memories": memories,
        "jobs": conn.execute(
            "SELECT COUNT(*) FROM consolidation_jobs WHERE source_event_id LIKE ? OR job_id LIKE ?", (like, like)
        ).fetchone()[0],
        "reviews": len(reviews),
        "human_reviewed": human,
        "promoted": promoted,
        "threads_only": [k for k, convs in threads if all(MARK in c for c in json.loads(convs))],
        "threads_shared": [k for k, convs in threads if not all(MARK in c for c in json.loads(convs))],
        "files": _mirror_files(),
    }


def _strip_shared_thread(conn: sqlite3.Connection, thread_key: str) -> None:
    row = conn.execute(
        "SELECT conversation_ids_json, event_ids_json, episode_count, last_seen_event_id FROM reasoning_threads WHERE thread_key = ?",
        (thread_key,),
    ).fetchone()
    convs = [c for c in json.loads(row[0]) if MARK not in c]
    events = [e for e in json.loads(row[1]) if MARK not in e]
    removed = conn.execute(
        "SELECT COUNT(*) FROM derived_memories WHERE thread_key = ? AND memory_id LIKE ?", (thread_key, f"%{MARK}%")
    ).fetchone()[0]
    last_id, last_at = row[3], None
    if last_id and MARK in last_id and events:
        last_id = events[-1]
        hit = conn.execute("SELECT observed_at FROM events WHERE event_id = ?", (last_id,)).fetchone()
        last_at = hit[0] if hit else None
    conn.execute(
        """UPDATE reasoning_threads SET conversation_ids_json = ?, event_ids_json = ?,
           episode_count = MAX(episode_count - ?, 0), last_seen_event_id = ?,
           last_seen_at = COALESCE(?, last_seen_at), updated_at = ? WHERE thread_key = ?""",
        (json.dumps(convs), json.dumps(events), removed, last_id, last_at, datetime.now().isoformat(), thread_key),
    )


def apply(conn: sqlite3.Connection, p: dict, db_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = ROOT / "imports" / "backups" / f"discard-subagent-{stamp}"
    backup.mkdir(parents=True)
    conn.execute(f"VACUUM INTO '{backup / 'journal.db'}'")
    for folder in ("episode-proposals", "doc-proposals"):
        if (ROOT / folder).is_dir():
            shutil.copytree(ROOT / folder, backup / folder)

    like = f"%{MARK}%"
    mems = p["memories"]
    marks = ",".join("?" * len(mems))
    with conn:
        for key in p["threads_shared"]:
            _strip_shared_thread(conn, key)
        for key in p["threads_only"]:
            conn.execute("DELETE FROM reasoning_threads WHERE thread_key = ?", (key,))
        if mems:
            conn.execute(f"DELETE FROM reviews WHERE memory_id IN ({marks})", mems)
            conn.execute(f"DELETE FROM review_audit WHERE memory_id IN ({marks})", mems)
            conn.execute(f"DELETE FROM derived_memories WHERE memory_id IN ({marks})", mems)
        conn.execute("DELETE FROM consolidation_jobs WHERE source_event_id LIKE ? OR job_id LIKE ?", (like, like))
    for path in p["files"]:
        path.unlink()
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--db", type=Path, default=DEFAULT_JOURNAL_PATH)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    p = plan(conn)
    print(f"derived memories: {len(p['memories'])}  jobs: {p['jobs']}  reviews: {p['reviews']}  "
          f"threads (subagent-only / shared): {len(p['threads_only'])} / {len(p['threads_shared'])}  files: {len(p['files'])}")
    if p["human_reviewed"] or p["promoted"]:
        print(f"REFUSING: {len(p['human_reviewed'])} reviewed by a person, {p['promoted']} promoted.")
        return 1
    if not args.apply:
        print("dry run; re-run with --apply")
        return 0
    backup = apply(conn, p, args.db)
    print(f"applied; backup at {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
