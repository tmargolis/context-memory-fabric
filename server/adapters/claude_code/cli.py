"""Claude Code / Desktop Code-tab adapter CLI.

    python -m server.adapters.claude_code.cli status
    python -m server.adapters.claude_code.cli tail --once
    python -m server.adapters.claude_code.cli backfill --all-projects [--since ISO8601]

`backfill --all-projects` defaults its cutoff to CMF_CLAUDE_CODE_BACKFILL_SINCE
if set, per docs/plan-active.md's MS4b dedup mitigation: a full-history
backfill would re-walk conversations already imported (under harness
"claude"/"gemini"/"chatgpt") through their own importers' cutoffs, and
compute_event_id's dedup doesn't catch that cross-importer collision because
harness is part of the hash. `--since` on the command line overrides the
env var for a one-off run; neither set means "no cutoff, tail everything".
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys

from server.adapters.claude_code.transcript_reader import (
    DEFAULT_PROJECTS_ROOT,
    TailStateStore,
    discover_transcript_files,
)
from server.adapters.claude_code.worker import process_pending, stats_summary
from server.consolidation.store import ConsolidationStore
from server.journal.store import SqliteEventStore


def _print_json(obj: object) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def _parse_since(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def cmd_status(args: argparse.Namespace) -> int:
    files = discover_transcript_files(Path(args.projects_root) if args.projects_root else None)
    with TailStateStore(args.db) as tail_store:
        known = set(tail_store.all_known_files())
    _print_json(
        {
            "transcript_files_found": len(files),
            "files_previously_tailed": len(known),
            "files_never_tailed": len([f for f in files if str(f.path) not in known]),
            "projects_root": str(args.projects_root or DEFAULT_PROJECTS_ROOT),
        }
    )
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    with SqliteEventStore(args.db) as store:
        stats = process_pending(
            store,
            ConsolidationStore(args.db),
            projects_root=Path(args.projects_root) if args.projects_root else None,
            reasoning_auto_accept_threshold=args.auto_accept_threshold,
            run_consolidation=not args.no_consolidation,
        )
    _print_json(stats_summary(stats))
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    if not args.all_projects:
        print("backfill currently only supports --all-projects", file=sys.stderr)
        return 2

    since_raw = args.since or os.getenv("CMF_CLAUDE_CODE_BACKFILL_SINCE")
    since = _parse_since(since_raw) if since_raw else None

    with SqliteEventStore(args.db) as store:
        stats = process_pending(
            store,
            ConsolidationStore(args.db),
            projects_root=Path(args.projects_root) if args.projects_root else None,
            reasoning_auto_accept_threshold=args.auto_accept_threshold,
            run_consolidation=not args.no_consolidation,
            since=since,
        )
    result = stats_summary(stats)
    result["cutoff_used"] = since.isoformat() if since else None
    _print_json(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude_code_cli")
    parser.add_argument("--db", type=Path, default=None, help="journal.db path override")
    parser.add_argument("--projects-root", type=Path, default=None, help="~/.claude/projects override (testing)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Show discovered transcripts and tail state")
    p_status.set_defaults(func=cmd_status)

    p_tail = sub.add_parser("tail", help="Tail changed transcripts once and journal new events")
    p_tail.add_argument("--once", action="store_true", help="accepted for CLI-shape compatibility; this command always runs one pass")
    p_tail.add_argument("--auto-accept-threshold", type=float, default=None)
    p_tail.add_argument("--no-consolidation", action="store_true")
    p_tail.set_defaults(func=cmd_tail)

    p_backfill = sub.add_parser("backfill", help="Backfill from existing transcripts")
    p_backfill.add_argument("--all-projects", action="store_true")
    p_backfill.add_argument("--since", type=str, default=None, help="ISO8601 cutoff; overrides CMF_CLAUDE_CODE_BACKFILL_SINCE")
    p_backfill.add_argument("--auto-accept-threshold", type=float, default=None)
    p_backfill.add_argument("--no-consolidation", action="store_true")
    p_backfill.set_defaults(func=cmd_backfill)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
