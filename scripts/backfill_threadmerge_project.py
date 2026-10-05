"""Backfill `project` on staged thread-merge episodes (2026-10-03).

Until b28446a, `_merge_tier1_by_thread` wrote every `reason:threadmerge:*`
episode with project NULL, while the window-level children it folded in
(rejected by `pipeline-thread-merge`, reason "merged into <id>") kept theirs.
Promotion tags FalkorDB from `derived_memories.project`, so these must be
fixed before they are promoted.

For every NOT-yet-reviewed episode with project NULL:

  1. a thread merge whose children carry exactly one non-null project gets
     that project (children disagreeing -> left alone and reported);
  2. otherwise, if `--override <conversation_id>=<project>` names its
     conversation, it gets the override. Overrides also set `source_project`
     on that conversation's pending doc proposals that have none.

Everything else is reported and left NULL. Already-reviewed episodes are
never touched (an approved/rejected row is history).

Writes `derived_memories.project` plus the matching episode mirror
(`"project"`) and doc proposal (`"source_project"`) files.

Dry run by default (read-only). `--apply` first backs up journal.db
(sqlite backup API) and tars both mirror dirs into imports/journal/bak/,
then makes every DB change in one transaction.

    uv run python scripts/backfill_threadmerge_project.py
    uv run python scripts/backfill_threadmerge_project.py --override <conv>=proj-alpha
    uv run python scripts/backfill_threadmerge_project.py --apply
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import tarfile
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from server.journal.store import DEFAULT_JOURNAL_PATH  # noqa: E402

MERGE_PREFIX = "reason:threadmerge:"
MERGE_REVIEWER = "pipeline-thread-merge"
EPISODE_DIR = _ROOT / "episode-proposals"
DOC_DIR = _ROOT / "doc-proposals"
BAK_DIR = _ROOT / "imports" / "journal" / "bak"


def parse_overrides(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in values:
        conv, sep, project = v.partition("=")
        if not sep or not conv.strip() or not project.strip():
            raise SystemExit(f"bad --override {v!r}: expected <conversation_id>=<project>")
        out[conv.strip()] = project.strip()
    return out


def _pending_mirrors(episode_dir: Path) -> dict[str, tuple[Path, dict[str, Any]]]:
    """memory_id -> (path, data) for mirrors at a tier root (not yet reviewed)."""
    out: dict[str, tuple[Path, dict[str, Any]]] = {}
    for tier in ("tier1", "tier2"):
        d = episode_dir / tier
        if not d.is_dir():
            continue
        for path in d.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out[data["memory_id"]] = (path, data)
    return out


def _conversation_of(memory_id: str, mirror: Optional[dict[str, Any]]) -> Optional[str]:
    if mirror and mirror.get("conversation_id"):
        return mirror["conversation_id"]
    if memory_id.startswith(MERGE_PREFIX):
        return memory_id[len(MERGE_PREFIX):].split(":", 1)[0]
    return None


def plan(conn: sqlite3.Connection, episode_dir: Path, doc_dir: Path, overrides: dict[str, str]) -> dict[str, Any]:
    reviewed = {
        mid for (mid,) in conn.execute(
            "SELECT memory_id FROM reviews WHERE review_state IN ('approved', 'rejected')"
        )
    }
    children: dict[str, set[Optional[str]]] = defaultdict(set)
    for reason, project in conn.execute(
        "SELECT r.reason, d.project FROM reviews r JOIN derived_memories d USING (memory_id) WHERE r.reviewer = ?",
        (MERGE_REVIEWER,),
    ):
        if reason and reason.startswith("merged into "):
            children[reason[len("merged into "):]].add(project)

    mirrors = _pending_mirrors(episode_dir)
    updates: list[tuple[str, str, str]] = []  # (memory_id, project, source)
    outcome: Counter = Counter()
    unresolved_convs: Counter = Counter()
    conflicts: list[tuple[str, list[str]]] = []
    matched_overrides: set[str] = set()

    rows = conn.execute("SELECT memory_id FROM derived_memories WHERE project IS NULL").fetchall()
    for (mid,) in rows:
        if mid in reviewed:
            continue
        is_merge = mid.startswith(MERGE_PREFIX)
        if is_merge:
            known = sorted(p for p in children.get(mid, set()) if p)
            if len(known) == 1:
                updates.append((mid, known[0], "children"))
                outcome["merge: from children"] += 1
                continue
            if len(known) > 1:
                conflicts.append((mid, known))
                outcome["merge: children disagree (skipped)"] += 1
                continue
        conv = _conversation_of(mid, mirrors.get(mid, (None, None))[1])
        if not is_merge and conv not in overrides:
            # Tens of thousands of legacy rows (older policies, imports) were
            # never reviewed and have no project; only overrides reach them.
            continue
        if conv in overrides:
            updates.append((mid, overrides[conv], "override"))
            matched_overrides.add(conv)
            outcome[f"{'merge' if is_merge else 'episode'}: override"] += 1
        else:
            outcome[f"{'merge' if is_merge else 'episode'}: no project found (left NULL)"] += 1
            unresolved_convs[conv or "<unknown>"] += 1

    mirror_hits = [(mirrors[mid][0], project) for mid, project, _ in updates if mid in mirrors]

    doc_hits: list[tuple[Path, str]] = []
    if overrides and doc_dir.is_dir():
        for path in doc_dir.glob("prop_*.json"):  # flat root = pending_review
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            conv = data.get("source_conversation_id")
            if conv in overrides and not data.get("source_project"):
                doc_hits.append((path, overrides[conv]))
                matched_overrides.add(conv)

    return {
        "updates": updates,
        "outcome": outcome,
        "by_project": Counter(p for _, p, _ in updates),
        "conflicts": conflicts,
        "unresolved_convs": unresolved_convs,
        "mirror_hits": mirror_hits,
        "doc_hits": doc_hits,
        "unknown_overrides": sorted(set(overrides) - matched_overrides),
    }


def report(p: dict[str, Any]) -> None:
    print(f"== episodes to update: {len(p['updates'])}  (pending mirror files found: {len(p['mirror_hits'])})")
    for k, v in sorted(p["outcome"].items()):
        print(f"   {v:5}  {k}")
    print("== by project:")
    for k, v in p["by_project"].most_common():
        print(f"   {v:5}  {k}")
    if p["conflicts"]:
        print(f"== conflicts ({len(p['conflicts'])}):")
        for mid, ps in p["conflicts"]:
            print(f"   {mid}  {ps}")
    if p["unresolved_convs"]:
        print(f"== thread merges still without a project, by conversation ({len(p['unresolved_convs'])}):")
        for conv, n in p["unresolved_convs"].most_common():
            print(f"   {n:3}  {conv}")
    print(f"== doc proposals to set source_project: {len(p['doc_hits'])}")
    if p["unknown_overrides"]:
        print(f"== overrides that matched nothing: {p['unknown_overrides']}")


def backup_state(conn: sqlite3.Connection, dirs: tuple[Path, ...], bak_dir: Path, label: str) -> None:
    """Back up journal.db (sqlite backup API) and tar each dir into bak_dir."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{label}"
    bak_dir.mkdir(parents=True, exist_ok=True)
    bak_db = bak_dir / f"journal-{stamp}.db"
    if bak_db.exists():
        raise SystemExit(f"ABORT: backup {bak_db} already exists -- refusing to overwrite it")
    with sqlite3.connect(bak_db) as dst:
        conn.backup(dst)
    print(f"backed up journal -> {bak_db}")
    for d in dirs:
        if d.is_dir():
            tar_path = bak_dir / f"{d.name}-{stamp}.tar.gz"
            with tarfile.open(tar_path, "w:gz") as tar:
                tar.add(d, arcname=d.name)
            print(f"backed up {d.name}/ -> {tar_path}")


def write_json_field(hits: list[tuple[Path, str]], field: str) -> None:
    for path, value in hits:
        data = json.loads(path.read_text(encoding="utf-8"))
        data[field] = value
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"rewrote {len(hits)} files ({field})")


def apply(conn: sqlite3.Connection, p: dict[str, Any], episode_dir: Path, doc_dir: Path, bak_dir: Path) -> None:
    if not p["updates"] and not p["doc_hits"]:
        print("nothing to write -- no backup taken")
        return
    backup_state(conn, (episode_dir, doc_dir), bak_dir, "pre-threadmerge-project")

    with conn:  # one transaction
        conn.executemany(
            "UPDATE derived_memories SET project = ? WHERE memory_id = ? AND project IS NULL",
            [(project, mid) for mid, project, _ in p["updates"]],
        )
    print(f"journal transaction committed ({len(p['updates'])} rows)")

    write_json_field(p["mirror_hits"], "project")
    write_json_field(p["doc_hits"], "source_project")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_JOURNAL_PATH)
    ap.add_argument("--episode-dir", type=Path, default=EPISODE_DIR)
    ap.add_argument("--doc-dir", type=Path, default=DOC_DIR)
    ap.add_argument("--bak-dir", type=Path, default=BAK_DIR)
    ap.add_argument("--override", action="append", default=[], metavar="CONV=PROJECT",
                    help="project for a conversation whose items have none (repeatable)")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args(argv)

    overrides = parse_overrides(args.override)
    uri = f"file:{args.db}" + ("" if args.apply else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True)
    try:
        p = plan(conn, args.episode_dir, args.doc_dir, overrides)
        report(p)
        if args.apply:
            apply(conn, p, args.episode_dir, args.doc_dir, args.bak_dir)
        else:
            print("\n(dry run -- nothing written; pass --apply to write)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
