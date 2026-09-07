"""Project buckets — the MS6 review unit.

MS6's plan made the *thread* the unit of review. The production journal
refutes that: the 315 unpromoted tier-1 episodes fall into 215 threads, 73%
of them singletons — a 1.47x reduction. The cause is structural, not a
tuning problem: `thread_key` is a free-text slug the extraction model
invents per window and matches by exact normalised-key equality
(server.consolidation.threads), so `openclaw-gateway-connection` and
`openclaw-gateway-setup` are two threads. Corpus-wide that is 1.9 episodes
per thread, and no amount of queue design improves it.

What actually costs review time is *re-orientation*, not keystrokes: a
215-entry queue makes a reviewer load a fresh mental frame 215 times.
Grouping the same 315 episodes into ~15 project buckets of ~20 related
items each does not reduce reading, but it reduces re-orientation by an
order of magnitude — every item in a batch shares a frame.

The taxonomy is a deliberate hand-written keyword map rather than
embeddings or a model call: it runs in milliseconds, it is inspectable and
correctable in one place, and a misfiled episode costs a reviewer nothing
(it appears in `misc`, which is reviewed like any other bucket). Rules are
ordered and first-match-wins, so put specific patterns before general ones
— `career-navigator` must beat the bare `claude` rule, and `agent-fabric`
must beat `agent`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Optional

MISC = "misc"

# The taxonomy is personal — it names a specific person's projects — so it
# lives in a gitignored file rather than in source. Search order:
#   1. an explicit `rules` argument (tests, callers with their own taxonomy)
#   2. $CMF_TAXONOMY
#   3. taxonomy.local.json at the repo root   <- gitignored, the normal case
#   4. nothing: every episode classifies as `misc`
#
# Falling back to `misc` rather than to a built-in default is deliberate. A
# shipped default would be someone else's project list quietly mislabelling
# this user's corpus, and a mislabelled bucket is worse than an honest
# `misc` — the reviewer can see and fix `misc`.
#
# Format (order matters — first match wins):
#   {"rules": [["bucket-name", "regex"], ...]}
DEFAULT_TAXONOMY_FILENAME = "taxonomy.local.json"
_REPO_ROOT = Path(__file__).resolve().parents[2]

_cache: dict[str, list[tuple[str, re.Pattern[str]]]] = {}


def taxonomy_path(path: Optional[Path] = None) -> Optional[Path]:
    """Resolve which taxonomy file is in play, or None if there isn't one."""
    if path is not None:
        return Path(path)
    env = os.environ.get("CMF_TAXONOMY")
    if env:
        return Path(env)
    local = _REPO_ROOT / DEFAULT_TAXONOMY_FILENAME
    return local if local.exists() else None


def load_rules(
    path: Optional[Path] = None, rules: Optional[list] = None
) -> list[tuple[str, re.Pattern[str]]]:
    """Compile the ordered (bucket, pattern) rules. Cached per file path."""
    if rules is not None:
        return [(b, re.compile(pat, re.I)) for b, pat in rules]

    resolved = taxonomy_path(path)
    if resolved is None:
        return []
    key = str(resolved)
    if key not in _cache:
        try:
            raw = json.loads(resolved.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise ValueError(f"could not read taxonomy at {resolved}: {e}") from e
        entries = raw.get("rules", raw) if isinstance(raw, dict) else raw
        _cache[key] = [(b, re.compile(pat, re.I)) for b, pat in entries]
    return _cache[key]


def clear_cache() -> None:
    """Drop compiled rules — call after editing the taxonomy file."""
    _cache.clear()


_THREAD_RE = re.compile(r"thread=([^\s|]+)")


def parse_thread_key(reason: Optional[str]) -> Optional[str]:
    """Recover the thread slug from a `derived_memories.reason` blob.

    MS3.5 serialised it as `| thread=<slug>` rather than a column
    (server.consolidation.store.record_consolidation). This exists to
    backfill the real column; nothing on the read path should call it.
    """
    if not reason:
        return None
    m = _THREAD_RE.search(reason)
    return m.group(1) if m else None


def classify(
    thread_key: Optional[str],
    statement: Optional[str] = None,
    compiled: Optional[list[tuple[str, re.Pattern[str]]]] = None,
) -> str:
    """Assign one project bucket. Slug wins over statement — the slug is the
    model's own topic label and is far less noisy than prose."""
    compiled = load_rules() if compiled is None else compiled
    for haystack in (thread_key or "", statement or ""):
        if not haystack:
            continue
        text = haystack.replace("_", "-")
        for bucket, pattern in compiled:
            if pattern.search(text):
                return bucket
    return MISC


def backfill(
    conn: sqlite3.Connection,
    policy_name: str = "reasoning-episode",
    dry_run: bool = True,
    taxonomy: Optional[Path] = None,
    rules: Optional[list] = None,
) -> dict[str, Any]:
    """Populate `thread_key` + `project` for rows that lack them.

    Dry-run by default, like every other mutating helper in
    server.review — the caller opts in to writing. An additive,
    idempotent backfill is the least dangerous write in the package,
    which is exactly why it was the one place the guard got skipped;
    a safety story with one silent exception is not a safety story.

    Idempotent: keyed on `project IS NULL` alone, because `project` is
    always assigned (`misc` is the floor) whereas `thread_key` is
    legitimately NULL for an episode whose `reason` carries no `thread=`
    field — keying on that too would re-select the same row on every run
    and never converge.
    """
    rows = conn.execute(
        "SELECT memory_id, reason, statement, thread_key FROM derived_memories "
        "WHERE policy_name = ? AND project IS NULL",
        (policy_name,),
    ).fetchall()

    compiled = load_rules(taxonomy, rules)
    updates: list[tuple[str, str, str]] = []
    for row in rows:
        thread_key = row["thread_key"] or parse_thread_key(row["reason"])
        updates.append((thread_key or "", classify(thread_key, row["statement"], compiled), row["memory_id"]))

    if not dry_run and updates:
        conn.executemany(
            "UPDATE derived_memories SET thread_key = NULLIF(?, ''), project = ? WHERE memory_id = ?",
            updates,
        )
        conn.commit()

    buckets: dict[str, int] = {}
    for _, project, _ in updates:
        buckets[project] = buckets.get(project, 0) + 1
    return {
        "taxonomy": str(taxonomy_path(taxonomy)) if rules is None else "<explicit rules>",
        "rule_count": len(compiled),
        "rows_considered": len(rows),
        "rows_updated": 0 if dry_run else len(updates),
        "dry_run": dry_run,
        "buckets": dict(sorted(buckets.items(), key=lambda kv: -kv[1])),
    }
