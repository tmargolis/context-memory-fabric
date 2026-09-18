"""episode-proposals/: a human-browsable file mirror for staged reasoning
episodes (Backlog, "Proposal-directory housekeeping," 2026-09-18).

Staged reasoning episodes (the output of MS3.5's windowed extraction, and
MS4a2's `capture_session`) have always lived only as `derived_memories`
rows in SQLite (`imports/journal/journal.db`) -- unlike wiki proposals,
which are one JSON file per proposal under `wiki-proposals/`. This module
adds a write-through file projection alongside SQLite, which stays the
sole source of truth: job tracking, `supersedes` lineage, and idempotency
all depend on it, and nothing here is meant to be hand-edited as an
alternate input path. Review still happens via `approve_episode`/
`reject_episode` (or the review CLI) -- the mirror is read-only/
informational, same convention `wiki-proposals/`'s files follow once
MS6d's review/apply split exists for them too.

Layout: `episode-proposals/{tier1,tier2}/{sha256(memory_id)}.json` (a
memory_id can be long enough to exceed a filesystem's filename limit --
hit for real backfilling the production journal; the real memory_id is
still the first field inside the file), each with its own
`approved/`/`rejected/` subfolder once a human decides via
`approve_episode`/`reject_episode` -- tier split matches
`tier1_review_queue()`'s own prioritization (TIER1_KINDS vs. everything
else), so the smaller, higher-priority set doesn't get buried under the
larger one. `defer_episode` leaves a mirror in place at the tier root --
deferred is not terminal.

Deliberately unconditional in `ConsolidationStore.record_reasoning_episode()`
(the only caller): heuristic-pattern rows are written via the separate
`record_consolidation()` method and never reach this path at all, so there
is no policy_name to filter on here -- every row `record_reasoning_episode()`
ever sees is windowed reasoning-episode-shaped, whether from the offline
pipeline (`policy_name="reasoning-episode"`) or MS4a2's live capture
(`policy_name="cowork_live_v1"`).
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

TIER1_KINDS = frozenset({"decision", "plan", "retrospective", "rejected_alternative"})


def _mirror_filename(memory_id: str) -> str:
    """A memory_id can be arbitrarily long (windowed-episode ids embed the
    window's own first/last event_id, some of which are themselves long
    composite strings) -- long enough to exceed a filesystem's filename
    limit in practice (hit for real backfilling the production journal,
    2026-09-18). Hash it for the filename; the real memory_id is still the
    first field in the file's own JSON content."""
    return hashlib.sha256(memory_id.encode("utf-8")).hexdigest() + ".json"


def get_episode_proposals_dir(custom_dir: Optional[Path] = None) -> Path:
    """Same CMF_STATE_DIR/default-to-project-root convention as
    server.proposals.get_proposals_dir()."""
    if custom_dir is not None:
        p = custom_dir
    else:
        env_state_dir = os.getenv("CMF_STATE_DIR")
        if env_state_dir:
            p = Path(env_state_dir).expanduser().resolve() / "episode-proposals"
        else:
            project_root = Path(__file__).resolve().parent.parent
            p = project_root / "episode-proposals"
    p.mkdir(parents=True, exist_ok=True)
    return p


def tier_for_reasoning_kind(reasoning_kind: str) -> str:
    return "tier1" if reasoning_kind in TIER1_KINDS else "tier2"


def _locate_mirror(memory_id: str, root: Path) -> Optional[Path]:
    """Check every location a mirror file could currently be in: either
    tier's root (still pending/deferred) or either tier's approved/rejected
    subfolder."""
    filename = _mirror_filename(memory_id)
    for tier in ("tier1", "tier2"):
        for sub in (None, "approved", "rejected"):
            base = (root / tier / sub) if sub else (root / tier)
            candidate = base / filename
            if candidate.exists():
                return candidate
    return None


def write_episode_mirror(
    *,
    memory_id: str,
    reasoning_kind: str,
    statement: str,
    confidence: float,
    evidence_event_ids: list[str],
    policy_name: str,
    policy_version: str,
    approval_state: str,
    driving_question: Optional[str] = None,
    rationale: Optional[str] = None,
    thread_key: Optional[str] = None,
    base_dir: Optional[Path] = None,
) -> Path:
    """Write one staged episode's mirror file at its tier root. Called from
    ConsolidationStore.record_reasoning_episode() right after the real
    derived_memories row is written -- read-only projection, not a second
    write path.
    """
    root = get_episode_proposals_dir(base_dir)
    tier = tier_for_reasoning_kind(reasoning_kind)
    tier_dir = root / tier
    tier_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "memory_id": memory_id,
        "policy_name": policy_name,
        "policy_version": policy_version,
        "reasoning_kind": reasoning_kind,
        "tier": tier,
        "statement": statement,
        "confidence": confidence,
        "driving_question": driving_question,
        "rationale": rationale,
        "thread_key": thread_key,
        "evidence_event_ids": evidence_event_ids,
        "approval_state": approval_state,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "_note": "Read-only projection of a derived_memories row. Review via "
                 "approve_episode/reject_episode (or the review CLI) -- editing this "
                 "file does not change anything.",
    }

    path = tier_dir / _mirror_filename(memory_id)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def move_episode_mirror(memory_id: str, new_status: str, base_dir: Optional[Path] = None) -> Optional[Path]:
    """Move an existing mirror to its tier's approved/ or rejected/ subfolder.

    A no-op (returns None) if no mirror exists for this memory_id -- e.g. a
    heuristic-pattern memory_id, which is never mirrored, or an id from
    before this module existed.
    """
    if new_status not in ("approved", "rejected"):
        raise ValueError(f"new_status must be 'approved' or 'rejected', got {new_status!r}")

    root = get_episode_proposals_dir(base_dir)
    existing = _locate_mirror(memory_id, root)
    if existing is None:
        return None

    tier = existing.parents[0].name if existing.parents[0].name in ("tier1", "tier2") else existing.parents[1].name
    target_dir = root / tier / new_status
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _mirror_filename(memory_id)

    if existing != target:
        existing.rename(target)
    return target


def backfill_from_rows(rows: list[dict[str, Any]], base_dir: Optional[Path] = None) -> dict[str, int]:
    """One-time backfill for episodes staged before this module existed.
    Each row is a dict shaped like a `derived_memories` row (memory_id,
    reasoning_kind, statement, confidence, evidence_event_ids (list),
    policy_name, policy_version, approval_state).

    Scoped deliberately by the caller, not by this function: the real
    backlog (2026-09-18) is 25,961 heuristic-pattern rows (never mirrored,
    excluded structurally -- see module docstring) plus 1,243
    reasoning-episode@0.2 rows (301 tier1 / 942 tier2) -- callers should
    pass tier1 first, tier2 as a second pass, not one combined dump.
    """
    written = 0
    for row in rows:
        write_episode_mirror(
            memory_id=row["memory_id"],
            reasoning_kind=row["reasoning_kind"],
            statement=row["statement"],
            confidence=row["confidence"],
            evidence_event_ids=row["evidence_event_ids"],
            policy_name=row["policy_name"],
            policy_version=row["policy_version"],
            approval_state=row["approval_state"],
            driving_question=row.get("driving_question"),
            rationale=row.get("rationale"),
            thread_key=row.get("thread_key"),
            base_dir=base_dir,
        )
        written += 1
    return {"written": written}
