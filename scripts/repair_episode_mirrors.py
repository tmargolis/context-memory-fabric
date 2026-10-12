"""Audit/repair JSON projections without changing journal data or review verdicts.

Default is read-only; --apply restores missing mirrors and synchronizes existing
mirrors' review state/location from the journal. Heuristic memories are excluded.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

from server.episode_proposals import _mirror_filename, tier_for_reasoning_kind


def repair(journal: Path, root: Path, *, apply: bool = False) -> dict:
    # Never instantiate a store: its constructor can migrate operational state.
    with sqlite3.connect(journal.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("""
            SELECT d.*, r.review_state, r.reviewer, r.reviewed_at,
                   r.reason AS review_reason, e.conversation_id, e.harness
            FROM derived_memories d LEFT JOIN reviews r USING(memory_id)
            LEFT JOIN events e ON e.event_id = d.source_event_id
            WHERE d.reasoning_kind IS NOT NULL ORDER BY d.created_at
        """)]
    files = {}
    for path in root.rglob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        mid = data["memory_id"]
        if mid in files:
            raise ValueError(f"Duplicate mirror for {mid}: {files[mid][0]} and {path}")
        files[mid] = (path, data)
    changes = []
    for row in rows:
        mid = row["memory_id"]
        # Deferred reviews remain staged; superseded derivations stay superseded.
        status = row["approval_state"]
        if row["review_state"] in ("approved", "rejected"):
            status = row["review_state"]
        old, data = files.get(mid, (None, None))
        tier = tier_for_reasoning_kind(row["reasoning_kind"])
        target = root / tier
        if status in ("approved", "rejected"):
            target /= status
        target /= _mirror_filename(mid)
        if data is None:
            # The serialized reason is preserved verbatim rather than guessing
            # boundaries between Q/why/alternatives in its free-text format.
            data = {k: row[k] for k in (
                "memory_id", "policy_name", "policy_version", "reasoning_kind",
                "statement", "confidence", "thread_key", "project",
                "conversation_id", "harness",
            )}
            data.update(tier=tier, driving_question=None, rationale=None,
                        reason=row["reason"],
                        evidence_event_ids=json.loads(row["evidence_event_ids_json"] or "[]"),
                        written_at=row["created_at"],
                        _note="Read-only projection of a derived_memories row. Review via CMF tools; editing this file does not change the journal.")
        expected_review = {k: row[k] for k in ("reviewer", "reviewed_at", "review_reason")
                           if row[k] is not None}
        needs_sync = old is None or old != target or data.get("approval_state") != status
        if not needs_sync:
            continue
        changes.append({"memory_id": mid, "action": "restore" if old is None else "synchronize",
                        "status": status, "path": str(target)})
        if apply:
            data.update(approval_state=status, **expected_review)
            target.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replacement; never expose a partial JSON file to MCP readers.
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            temporary.replace(target)
            if old is not None and old != target:
                old.unlink()
    return {"reasoning_rows": len(rows), "mirrors_before": len(files),
            "restore": sum(c["action"] == "restore" for c in changes),
            "synchronize": sum(c["action"] == "synchronize" for c in changes),
            "applied": apply, "changes": changes}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(repair(args.journal, args.root, apply=args.apply), indent=2))


if __name__ == "__main__":
    main()
