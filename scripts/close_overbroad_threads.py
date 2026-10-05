"""Close over-broad open threads in reasoning_threads (extract@1.6 review follow-on).

extract@1.6 reused open thread labels across unrelated conversations.
This script finds open threads spanning > threshold conversations (default 10)
and marks their status as 'resolved'.

Usage:
    python scripts/close_overbroad_threads.py [--threshold 10] [--db imports/journal/journal.db] [--apply]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = _ROOT / "imports" / "journal" / "journal.db"


def find_overbroad_threads(db_path: Path, threshold: int = 10) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()
    rows = c.execute("""
        SELECT thread_key, title, status, episode_count, conversation_ids_json, last_seen_at
        FROM reasoning_threads
        WHERE status = 'open'
    """).fetchall()
    conn.close()

    candidates = []
    for key, title, status, ep_count, cids_json, last_seen in rows:
        try:
            cids = json.loads(cids_json)
        except Exception:
            cids = []
        if len(cids) > threshold:
            candidates.append({
                "thread_key": key,
                "title": title,
                "num_conversations": len(cids),
                "episode_count": ep_count,
                "last_seen_at": last_seen,
            })
    candidates.sort(key=lambda x: x["num_conversations"], reverse=True)
    return candidates


def apply_close_threads(db_path: Path, thread_keys: list[str]) -> int:
    conn = sqlite3.connect(str(db_path))
    now_iso = datetime.now(timezone.utc).isoformat()
    c = conn.cursor()
    c.executemany(
        "UPDATE reasoning_threads SET status = 'resolved', updated_at = ? WHERE thread_key = ?",
        [(now_iso, k) for k in thread_keys],
    )
    conn.commit()
    count = c.rowcount
    conn.close()
    return count


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--threshold", type=int, default=10, help="Minimum conversations to consider overbroad (default: 10)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to journal.db")
    ap.add_argument("--apply", action="store_true", help="Apply updates (default: dry-run)")
    args = ap.parse_args()

    candidates = find_overbroad_threads(args.db, args.threshold)
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"[{mode}] Found {len(candidates)} open threads spanning > {args.threshold} conversations:\n")
    print(f"{'Thread Key':<42} {'Convs':<8} {'Episodes':<10} {'Last Seen':<32} {'Title'}")
    print("-" * 110)
    for c in candidates:
        print(f"{c['thread_key']:<42} {c['num_conversations']:<8} {c['episode_count']:<10} {str(c['last_seen_at']):<32} {c['title'][:40]}")

    if not args.apply:
        print(f"\nDry run complete. Use --apply to update {len(candidates)} threads in {args.db}.")
        return 0

    applied_count = apply_close_threads(args.db, [c["thread_key"] for c in candidates])
    print(f"\nApplied: updated {applied_count} threads to status='resolved' in {args.db}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
