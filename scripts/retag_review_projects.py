"""Re-derive and alias the project on items still awaiting review (2026-10-03).

Two fixes changed how a project is resolved after these items were staged:

  1. Cowork: `~/Documents/Claude/Projects/<p>` now maps to `<p>` (it had
     collapsed to `claude`), and `CMF_COWORK_SCHEDULED_TASK_PROJECTS` now
     names folderless scheduled tasks' projects. Each Cowork conversation's
     project is re-derived from its journaled `project_folder` and sidecar
     `scheduledTaskId`.
  2. `CMF_PROJECT_ALIASES` folds one slug into another (applied after 1).

Only NOT-yet-reviewed episodes (pending mirror files at the tier roots) and
pending doc proposals (flat doc-proposals/ root) are touched; anything
approved, rejected or promoted is history and stays as it is. A re-derived
value of None never clears an existing project. The journal's events keep
their original metadata (evidence is not rewritten).

Dry run by default (read-only). `--apply` backs up journal.db and both
mirror dirs first, then updates derived_memories in one transaction and
rewrites the matching mirror (`"project"`) / proposal (`"source_project"`)
files.

    uv run python scripts/retag_review_projects.py
    uv run python scripts/retag_review_projects.py --apply
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import re
from pathlib import Path
import sqlite3
import sys
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import server  # noqa: E402,F401  -- loads .env (CMF_* mappings)
from server.adapters.claude_cowork.discovery import (  # noqa: E402
    folder_project_map,
    project_for_folder,
    project_for_session,
    scheduled_task_projects,
)
from server.consolidation.project_aliases import project_aliases, resolve_project  # noqa: E402
from server.journal.store import DEFAULT_JOURNAL_PATH  # noqa: E402

from scripts.backfill_threadmerge_project import (  # noqa: E402
    BAK_DIR,
    DOC_DIR,
    EPISODE_DIR,
    _pending_mirrors,
    backup_state,
    write_json_field,
)

COWORK = "claude_cowork"
FOLDER_MAP: list = []  # set in main() from CMF_PROJECT_FOLDER_MAP


def _legacy_folder_project(folder: Optional[str]) -> Optional[str]:
    """What the pre-2026-10-03 rule produced: the first folder under ~/Dev or
    ~/Documents (so every ~/Documents/Claude/Projects/<p> became `claude`)."""
    if not folder:
        return None
    for root in ("Dev", "Documents"):
        prefix = f"{Path.home()}/{root}/"
        if folder.lower().startswith(prefix.lower()):
            rest = folder[len(prefix):].split("/", 1)[0]
            return re.sub(r"[^a-z0-9]+", "-", rest.lower()).strip("-") or None
    return None


def _cowork_project(conn: sqlite3.Connection, conv: str, tasks: dict[str, str], cache: dict):
    """(derived project, set of values an automatic rule could have stamped)."""
    if conv not in cache:
        row = conn.execute(
            "SELECT metadata_json FROM events WHERE conversation_id = ? AND harness = ? LIMIT 1", (conv, COWORK)
        ).fetchone()
        md = json.loads(row[0]) if row else {}
        folder, side = md.get("project_folder"), md.get("cowork") or {}
        derived = project_for_session(folder, side, tasks, FOLDER_MAP) if row else None
        auto = {None, md.get("project"), project_for_folder(folder), _legacy_folder_project(folder),
                tasks.get(side.get("scheduledTaskId") or "")}
        cache[conv] = (derived, auto)
    return cache[conv]


def _target(current: Optional[str], harness: Optional[str], conv: Optional[str], conn, tasks, aliases, cache):
    """Re-derive only values an automatic rule produced (a reviewer's or a
    consolidation's deliberate project is kept; use --set to change it), then
    apply aliases."""
    new = current
    if harness == COWORK and conv:
        derived, auto = _cowork_project(conn, conv, tasks, cache)
        if derived and (current in auto or resolve_project(current, aliases) in {resolve_project(a, aliases) for a in auto}):
            new = derived
    return resolve_project(new, aliases)


def plan(conn: sqlite3.Connection, episode_dir: Path, doc_dir: Path,
         tasks: dict[str, str], aliases: dict[str, str],
         explicit: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """`explicit` ({memory_id or proposal_id: project}) wins over derivation and
    may replace an existing project: it is a reviewer's per-item decision."""
    explicit = explicit or {}
    cache: dict[str, Optional[str]] = {}
    ep_updates: list[tuple[str, Path, str]] = []  # (memory_id, mirror path, new project)
    changes: Counter = Counter()
    for mid, (path, data) in _pending_mirrors(episode_dir).items():
        cur = data.get("project")
        new = explicit.get(mid) or _target(cur, data.get("harness"), data.get("conversation_id"), conn, tasks, aliases, cache)
        if new != cur:
            ep_updates.append((mid, path, new))
            changes[(cur, new)] += 1

    doc_hits: list[tuple[Path, str]] = []
    doc_changes: Counter = Counter()
    if doc_dir.is_dir():
        for path in doc_dir.glob("prop_*.json"):  # flat root = pending_review
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            cur = data.get("source_project")
            new = explicit.get(data.get("proposal_id")) or _target(
                cur, data.get("source_harness"), data.get("source_conversation_id"), conn, tasks, aliases, cache)
            if new != cur:
                doc_hits.append((path, new))
                doc_changes[(cur, new)] += 1
    return {"ep_updates": ep_updates, "changes": changes, "doc_hits": doc_hits, "doc_changes": doc_changes}


def report(p: dict[str, Any]) -> None:
    print(f"== pending episodes to retag: {len(p['ep_updates'])}")
    for (cur, new), n in p["changes"].most_common():
        print(f"   {n:5}  {cur} -> {new}")
    print(f"== pending doc proposals to retag: {len(p['doc_hits'])}")
    for (cur, new), n in p["doc_changes"].most_common():
        print(f"   {n:5}  {cur} -> {new}")


def apply(conn: sqlite3.Connection, p: dict[str, Any], episode_dir: Path, doc_dir: Path, bak_dir: Path) -> None:
    if not p["ep_updates"] and not p["doc_hits"]:
        print("nothing to write -- no backup taken")
        return
    backup_state(conn, (episode_dir, doc_dir), bak_dir, "pre-retag-projects")
    with conn:  # one transaction
        conn.executemany("UPDATE derived_memories SET project = ? WHERE memory_id = ?",
                         [(new, mid) for mid, _, new in p["ep_updates"]])
    print(f"journal transaction committed ({len(p['ep_updates'])} rows)")
    write_json_field([(path, new) for _, path, new in p["ep_updates"]], "project")
    write_json_field(p["doc_hits"], "source_project")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_JOURNAL_PATH)
    ap.add_argument("--episode-dir", type=Path, default=EPISODE_DIR)
    ap.add_argument("--doc-dir", type=Path, default=DOC_DIR)
    ap.add_argument("--bak-dir", type=Path, default=BAK_DIR)
    ap.add_argument("--set", action="append", default=[], metavar="ID=PROJECT",
                    help="per-item project for a pending episode (memory_id) or doc proposal (proposal_id); repeatable")
    ap.add_argument("--only-set", action="store_true", help="apply only the --set items, no derivation or aliases")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args(argv)

    tasks, aliases = scheduled_task_projects(), project_aliases()
    FOLDER_MAP[:] = folder_project_map()
    print(f"scheduled-task mappings: {len(tasks)}  folder mappings: {len(FOLDER_MAP)}  project aliases: {len(aliases)}")
    uri = f"file:{args.db}" + ("" if args.apply else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True)
    try:
        explicit = {}
        for item in args.set:
            k, sep, v = item.rpartition("=")
            if not sep or not k or not v:
                raise SystemExit(f"bad --set {item!r}: expected ID=PROJECT")
            explicit[k] = v
        if args.only_set:
            p = plan(conn, args.episode_dir, args.doc_dir, {}, {}, explicit)
            keep = set(explicit)
            p["ep_updates"] = [u for u in p["ep_updates"] if u[0] in keep]
            p["doc_hits"] = [h for h in p["doc_hits"] if json.loads(h[0].read_text())["proposal_id"] in keep]
            p["changes"] = Counter((None, n) for _, _, n in p["ep_updates"])
            p["doc_changes"] = Counter((None, n) for _, n in p["doc_hits"])
        else:
            p = plan(conn, args.episode_dir, args.doc_dir, tasks, aliases, explicit)
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
