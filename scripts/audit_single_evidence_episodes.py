"""MS6b Task 3d — audit promoted episodes with a single evidence turn for the
same defect class the 360-cam/eclipse correction found: a statement whose
subject doesn't survive a later turn in the same conversation (a "for a
friend, not me" correction, a wrong attribution, a walked-back conclusion)
that never made it into the episode's evidence.

Not a re-extraction — a cheap triage. For each promoted, approved,
single-evidence episode, pulls the next few user turns in the same
conversation after the evidence turn and flags ones containing a
subject-correction signal phrase. Flagged episodes need a human read (and
likely `correct-memory`); unflagged ones are not proven correct, just not
flagged by this heuristic.

Usage:
    uv run python scripts/audit_single_evidence_episodes.py [--db PATH]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sqlite3

from server.journal.store import DEFAULT_JOURNAL_PATH

SIGNAL_PATTERNS = [
    r"\bfor (?:a|my) friend\b", r"\bnot me\b", r"\bnot for me\b", r"\bnot my\b",
    r"\bfor (?:my )?(?:wife|husband|partner|brother|sister|mom|mother|dad|father|son|daughter|colleague|coworker|client|boss)\b",
    r"\bon behalf of\b", r"\bsomeone else\b", r"\bhe'll\b", r"\bshe'll\b", r"\bthey'll\b",
    r"\bactually,? (?:i|it'?s|this is)\b", r"\bi meant\b", r"\bcorrection\b", r"\bto clarify\b",
    r"\bnot .{0,15}(?:me|mine|myself)\b", r"\bsorry,? (?:i|that|meant)\b", r"\bgift for\b",
]
SIGNAL_RE = re.compile("|".join(SIGNAL_PATTERNS), re.IGNORECASE)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--graph-name", default="mem-fabric-local")
    parser.add_argument("--lookahead-turns", type=int, default=6)
    args = parser.parse_args()

    conn = sqlite3.connect(str(args.db or DEFAULT_JOURNAL_PATH))
    conn.row_factory = sqlite3.Row

    episodes = conn.execute(
        """
        SELECT dm.memory_id, dm.statement, dm.evidence_event_ids_json
        FROM derived_memories dm
        JOIN reviews r ON r.memory_id = dm.memory_id
        JOIN promotions p ON p.memory_id = dm.memory_id AND p.graph_name = ? AND p.status = 'succeeded'
        WHERE r.review_state = 'approved' AND json_array_length(dm.evidence_event_ids_json) = 1
        ORDER BY dm.memory_id
        """,
        (args.graph_name,),
    ).fetchall()

    print(f"Auditing {len(episodes)} single-evidence promoted episodes in {args.graph_name!r}...\n")

    flagged = []
    for ep in episodes:
        evidence_ids = json.loads(ep["evidence_event_ids_json"] or "[]")
        if not evidence_ids:
            continue
        ev_id = evidence_ids[0]
        ev = conn.execute(
            "SELECT conversation_id, event_date, harness FROM events WHERE event_id = ?", (ev_id,)
        ).fetchone()
        if ev is None or ev["conversation_id"] is None:
            continue

        following = conn.execute(
            """
            SELECT event_id, actor_type, json_extract(content_json, '$.text') AS txt
            FROM events
            WHERE conversation_id = ? AND event_date > ? AND actor_type = 'user'
            ORDER BY event_date
            LIMIT ?
            """,
            (ev["conversation_id"], ev["event_date"], args.lookahead_turns),
        ).fetchall()

        for turn in following:
            text = turn["txt"] or ""
            m = SIGNAL_RE.search(text)
            if m:
                flagged.append({
                    "memory_id": ep["memory_id"],
                    "statement": ep["statement"],
                    "signal": m.group(0),
                    "turn_text": text[:200],
                    "turn_event_id": turn["event_id"],
                })
                break

    print(f"Flagged {len(flagged)} of {len(episodes)} for human review:\n")
    for f in flagged:
        print(f"memory_id: {f['memory_id']}")
        print(f"  statement: {f['statement'][:160]}")
        print(f"  signal: {f['signal']!r} in turn {f['turn_event_id']}")
        print(f"  turn text: {f['turn_text']}")
        print()

    if not flagged:
        print("(none)")


if __name__ == "__main__":
    main()
