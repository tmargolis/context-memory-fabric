"""B10 activation tooling: preflight, consistent snapshot, thread-column migration.

Read-only unless told otherwise. Nothing here restarts a service, edits a
schedule, or changes a proposal or review verdict.

  preflight  Inventory what still depends on the pre-1.7 apply protections
             (force, the removed-fraction guard, trips_overwrite_check,
             rebuild_proposal_id): pending and approved-but-unapplied legacy
             updates, duplicate targets, stale bases, open review runs.
             Exit 0 = nothing depends on them; 3 = decisions needed first.
  snapshot   Consistent journal backup (SQLite online backup API, safe while
             pollers write) plus a tar of doc/episode proposal state and a
             sha256 manifest. Writes only under --out.
  migrate    Add + backfill reasoning_threads.projects_json, printing before/
             after counts. Runs against a scratch copy unless --live
             --approved is given; --live takes the write lock first so it
             serializes with the pollers.

Usage (from the repo root, with the venv):
  python scripts/b10_activation.py preflight [--out DIR]
  python scripts/b10_activation.py snapshot --out DIR
  python scripts/b10_activation.py migrate [--live --approved]
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _env() -> dict[str, str]:
    from dotenv import dotenv_values

    return {k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None}


def _journal() -> Path:
    from server.journal.store import DEFAULT_JOURNAL_PATH

    return Path(os.getenv("CMF_JOURNAL_PATH") or DEFAULT_JOURNAL_PATH)


def _state_dir() -> Path:
    return Path(os.getenv("CMF_STATE_DIR") or _env().get("CMF_STATE_DIR") or ROOT).expanduser()


def _wiki() -> Path:
    return Path(os.getenv("LLM_WIKI_PATH") or _env()["LLM_WIKI_PATH"]).expanduser()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- preflight ---------------------------------------------------------------


# The pre-1.7 apply guard, restated so preflight still runs once activation removes it.
UPDATE_MAX_REMOVED_FRACTION = 0.30
UPDATE_GUARD_MIN_LINES = 10


def removed_line_fraction(live_text: str, proposed_text: str) -> float:
    import difflib

    live = live_text.splitlines()
    if not live:
        return 0.0
    ops = difflib.SequenceMatcher(None, live, proposed_text.splitlines(), autojunk=False).get_opcodes()
    return sum(i2 - i1 for tag, i1, i2, _, _ in ops if tag in ("delete", "replace")) / len(live)


def preflight(out: Path | None) -> int:
    proposals_dir, wiki = _state_dir() / "proposals-docs", _wiki()
    open_items: list[dict] = []
    for path in sorted(proposals_dir.glob("*.json")) + sorted((proposals_dir / "approved").glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        status = raw.get("status")
        if status == "approved" and raw.get("applied_at"):
            continue
        if status not in ("pending_review", "approved"):
            continue
        live = wiki / raw.get("target_path", "")
        item = {
            "proposal_id": raw["proposal_id"], "status": status, "target_path": raw.get("target_path"),
            "operation": raw.get("operation"), "mode": raw.get("mode", "legacy-replace"),
            "conversation": raw.get("source_conversation_id"), "created_at": raw.get("created_at"),
            "file_sha256": _sha(path),
        }
        if live.is_file():
            text = live.read_text(encoding="utf-8", errors="replace")
            frac = removed_line_fraction(text, raw.get("proposed_content", ""))
            item.update(
                live_lines=len(text.splitlines()), rewrites_fraction=round(frac, 2),
                needs_force=len(text.splitlines()) >= UPDATE_GUARD_MIN_LINES and frac > UPDATE_MAX_REMOVED_FRACTION,
                stale_base=hashlib.sha256(text.encode("utf-8")).hexdigest() != raw.get("current_sha256"),
            )
        elif raw.get("operation") == "update":
            item.update(live_missing=True)
        else:
            item.update(create_target_exists=False)
        open_items.append(item)

    by_target = defaultdict(list)
    for item in open_items:
        by_target[item["target_path"]].append(item["proposal_id"])
    duplicates = {t: ids for t, ids in by_target.items() if len(ids) > 1}

    con = sqlite3.connect(f"file:{_journal()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    runs = [dict(r) for r in con.execute(
        "SELECT run_id, started_at, recorded_at, confirmed_at, item_count FROM review_runs WHERE confirmed_at IS NULL ORDER BY started_at")]
    rebuild_refs = [dict(r) for r in con.execute(
        "SELECT r.run_id, r.label, r.item_id, r.rebuild_proposal_id, r.status FROM review_recommendations r "
        "JOIN review_runs u ON u.run_id = r.run_id WHERE r.rebuild_proposal_id IS NOT NULL AND u.confirmed_at IS NULL")]
    historical_rebuilds = con.execute(
        "SELECT COUNT(*) FROM review_recommendations WHERE rebuild_proposal_id IS NOT NULL").fetchone()[0]
    con.close()

    legacy_updates = [i for i in open_items if i["mode"] == "legacy-replace" and i["operation"] == "update"]
    blockers = {
        "approved_unapplied_needing_force": [i["proposal_id"] for i in open_items if i["status"] == "approved" and i.get("needs_force")],
        "pending_legacy_updates_needing_force": [i["proposal_id"] for i in legacy_updates if i["status"] == "pending_review" and i.get("needs_force")],
        "unconfirmed_runs_with_rebuild_refs": sorted({r["run_id"] for r in rebuild_refs}),
    }
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "journal": str(_journal()), "proposals_dir": str(proposals_dir), "wiki": str(wiki),
        "counts": {
            "open_proposals": len(open_items),
            "by_status_mode_operation": Counter(f"{i['status']}/{i['mode']}/{i['operation']}" for i in open_items),
            "stale_base": sum(bool(i.get("stale_base")) for i in open_items),
            "needs_force": sum(bool(i.get("needs_force")) for i in open_items),
            "duplicate_targets": len(duplicates),
            "unconfirmed_review_runs": len(runs),
            "rebuild_refs_in_unconfirmed_runs": len(rebuild_refs),
            "historical_rebuild_refs (kept, read-only)": historical_rebuilds,
        },
        "blockers": blockers,
        "unconfirmed_runs": runs,
        "rebuild_refs": rebuild_refs,
        "duplicate_targets": duplicates,
        "open_proposals": open_items,
    }
    text = json.dumps(report, indent=2, default=str)
    if out:
        out.mkdir(parents=True, exist_ok=True)
        (out / "preflight.json").write_text(text + "\n")
    print(json.dumps({k: report[k] for k in ("counts", "blockers")}, indent=2, default=str))
    return 3 if any(blockers.values()) else 0


# --- snapshot ----------------------------------------------------------------


def snapshot(out: Path) -> int:
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty; refusing to mix snapshots")
    out.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{_journal()}?mode=ro", uri=True)
    dst = sqlite3.connect(out / "journal.db")
    src.backup(dst)  # one consistent read transaction, safe alongside writers
    dst.close()
    src.close()
    check = sqlite3.connect(out / "journal.db").execute("PRAGMA integrity_check").fetchone()[0]
    state = _state_dir()
    with tarfile.open(out / "proposal-state.tar.gz", "w:gz") as tar:
        for name in ("proposals-docs", "proposals-episode"):
            if (state / name).exists():
                tar.add(state / name, arcname=name)
    manifest = {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "journal_source": str(_journal()), "integrity_check": check,
        "files": {p.name: _sha(p) for p in sorted(out.iterdir()) if p.is_file()},
        "proposal_files": {str(p.relative_to(state)): _sha(p) for name in ("proposals-docs", "proposals-episode")
                           for p in sorted((state / name).rglob("*.json"))},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"out": str(out), "integrity_check": check, "proposal_files": len(manifest["proposal_files"])}, indent=2))
    return 0 if check == "ok" else 1


# --- migrate -----------------------------------------------------------------


def _thread_counts(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cols = {r[1] for r in con.execute("PRAGMA table_info(reasoning_threads)")}
    counts = {"threads": con.execute("SELECT COUNT(*) FROM reasoning_threads").fetchone()[0],
              "has_projects_json": "projects_json" in cols}
    if "projects_json" in cols:
        counts["with_projects"] = con.execute("SELECT COUNT(*) FROM reasoning_threads WHERE projects_json != '[]'").fetchone()[0]
        counts["multi_project"] = sum(len(json.loads(r[0])) > 1 for r in con.execute("SELECT projects_json FROM reasoning_threads"))
    con.close()
    return counts


def migrate(live: bool, approved: bool) -> int:
    from server.consolidation.threads import ThreadIndex

    journal = _journal()
    if live and not approved:
        raise SystemExit("--live needs --approved: the live migration is an activation step")
    with tempfile.TemporaryDirectory() as tmp:
        db = journal
        if not live:
            db = Path(tmp) / "journal.db"
            src = sqlite3.connect(f"file:{journal}?mode=ro", uri=True)
            dst = sqlite3.connect(db)
            src.backup(dst)
            dst.close()
            src.close()
        before = _thread_counts(db)
        if live:
            lock = sqlite3.connect(db, timeout=600)
            lock.execute("BEGIN IMMEDIATE")  # wait out an in-flight writer, then hold off new ones
            lock.rollback()
            lock.close()
        with ThreadIndex(db) as index:  # ALTER + backfill on first open, in one transaction
            rerun = index.backfill_projects()  # idempotency check: must update nothing
        after = _thread_counts(db)
        print(json.dumps({"target": "live" if live else "scratch copy", "before": before, "after": after,
                          "rerun_backfill": rerun}, indent=2))
        return 0 if rerun["updated"] == 0 and after["threads"] == before["threads"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["preflight", "snapshot", "migrate"])
    parser.add_argument("--out", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args()
    if args.command == "preflight":
        return preflight(args.out)
    if args.command == "snapshot":
        if not args.out:
            parser.error("snapshot needs --out")
        return snapshot(args.out)
    return migrate(args.live, args.approved)


if __name__ == "__main__":
    raise SystemExit(main())
