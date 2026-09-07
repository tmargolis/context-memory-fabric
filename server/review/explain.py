"""`explain(memory_id)` — why does this memory exist? (MS6, cheap half.)

MS6's plan defines `explain()` as walking episode -> derived_memory ->
source events -> journal turns -> thread, *and then* on into Graphiti for
the entities and edges it extracted. Those are two features wearing one
name, and only the first is needed to review anything:

  * The journal walk is a **review tool**. It is what turns an ambiguous
    episode from unresolvable into a ~2-minute decision, and it is what
    gets inlined into the review export so a verdict never needs a
    round-trip. Built here.
  * The Graphiti walk is the **differentiation demo** — "why does the
    system believe this?" answered all the way into the graph. It is
    deferred to MS6b: it has 26 promoted rows to run against today, and it
    is worth building when the graph has real content in it, which is what
    the review pass this module serves is for.

`reason` is unpacked back into its parts here because MS3.5 flattened the
episode's driving question, rationale, alternatives, status and thread into
one text blob (server.consolidation.store.record_consolidation). A reviewer
reading 315 of these needs the rationale on its own line, not inside a
pipe-delimited string.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Optional

# `reason` is built as "reasoning_kind=X | Q: ... | why: ... | alt: ... | status=... | thread=..."
_FIELD_RE = re.compile(r"(?:^|\s\|\s)(reasoning_kind|Q|why|alt|status|thread)[=:]\s*")

_LABELS = {
    "Q": "question",
    "why": "rationale",
    "alt": "alternatives",
    "status": "status",
    "thread": "thread_key",
    "reasoning_kind": "reasoning_kind",
}


def parse_reason(reason: Optional[str]) -> dict[str, str]:
    """Split a flattened `reason` blob back into named fields.

    Unknown or malformed input degrades to `{"raw": <text>}` rather than
    raising — this runs over 1,300 production rows written by three policy
    versions, and a reviewer would rather see the raw text than an error.
    """
    if not reason:
        return {}
    matches = list(_FIELD_RE.finditer(reason))
    if not matches:
        return {"raw": reason}
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(reason)
        out[_LABELS[m.group(1)]] = reason[m.end() : end].strip()
    return out


def resolve_evidence(
    conn: sqlite3.Connection, evidence_event_ids: list[str], max_chars: int = 1200
) -> list[dict[str, Any]]:
    """Resolve cited event_ids to the actual journal turns.

    Turns are truncated per-turn: the median episode cites 3 turns but the
    max is 20, and an un-truncated 20-turn dump is the thing that makes a
    reviewer skip the evidence entirely.
    """
    if not evidence_event_ids:
        return []
    qmarks = ",".join("?" * len(evidence_event_ids))
    rows = {
        r["event_id"]: r
        for r in conn.execute(
            f"SELECT event_id, actor_type, harness, event_date, observed_at, content_json "
            f"FROM events WHERE event_id IN ({qmarks})",
            evidence_event_ids,
        )
    }

    turns: list[dict[str, Any]] = []
    for event_id in evidence_event_ids:  # preserve the episode's own citation order
        row = rows.get(event_id)
        if row is None:
            # An episode may cite an event pruned by retention (MS2). Say so
            # rather than silently shortening the evidence list.
            turns.append({"event_id": event_id, "missing": True})
            continue
        try:
            content = json.loads(row["content_json"])
            text = content.get("text") or " ".join(
                b.get("text", "") for b in content.get("blocks", []) if isinstance(b, dict)
            )
        except (json.JSONDecodeError, AttributeError, TypeError):
            text = ""
        text = (text or "").strip()
        turns.append(
            {
                "event_id": event_id,
                "actor": row["actor_type"],
                "harness": row["harness"],
                "at": row["event_date"] or row["observed_at"],
                "text": text[:max_chars],
                "truncated": len(text) > max_chars,
            }
        )
    return turns


def explain(conn: sqlite3.Connection, memory_id: str, max_chars: int = 1200) -> Optional[dict[str, Any]]:
    """The complete review-time answer to "why does this memory exist"."""
    row = conn.execute("SELECT * FROM derived_memories WHERE memory_id = ?", (memory_id,)).fetchone()
    if row is None:
        return None

    keys = row.keys()
    evidence_ids = json.loads(row["evidence_event_ids_json"] or "[]") if "evidence_event_ids_json" in keys else []
    parsed = parse_reason(row["reason"])
    thread_key = (row["thread_key"] if "thread_key" in keys else None) or parsed.get("thread_key")

    # `reasoning_threads` is created by the MS3.5 consolidation run, not by
    # any store's schema — a journal that has only ever been consolidated
    # under the heuristic policy has no such table. Thread context is
    # enrichment, so its absence degrades the answer rather than failing it.
    thread = None
    if thread_key:
        try:
            t = conn.execute(
                "SELECT thread_key, title, status, episode_count, first_seen_at, last_seen_at "
                "FROM reasoning_threads WHERE thread_key = ?",
                (thread_key,),
            ).fetchone()
            thread = dict(t) if t else None
        except sqlite3.OperationalError:
            thread = None

    return {
        "memory_id": memory_id,
        "statement": row["statement"],
        "reasoning_kind": row["reasoning_kind"],
        "category": row["category"],
        "confidence": row["confidence"],
        "event_date": row["event_date"],
        "date_precision": row["date_precision"],
        "approval_state": row["approval_state"],
        "project": row["project"] if "project" in keys else None,
        "thread_key": thread_key,
        "thread": thread,
        # Unpacked rationale — the fields a reviewer actually reads.
        **{k: v for k, v in parsed.items() if k not in {"thread_key", "reasoning_kind"}},
        "evidence": resolve_evidence(conn, evidence_ids, max_chars=max_chars),
        "evidence_count": len(evidence_ids),
    }
