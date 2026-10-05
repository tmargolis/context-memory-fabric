"""Antigravity IDE adapter CLI.

    python -m server.adapters.antigravity.cli status
    python -m server.adapters.antigravity.cli tail --once
    python -m server.adapters.antigravity.cli backfill --all-app-dirs [--since ISO8601] [--until ISO8601]

`backfill --until` caps how far the tail offset advances (see worker.py's
process_pending docstring) -- used for the 2026-09-24 rollout's "capture
through last night only, leave today's live sessions for a later pass"
cutoff, so a later `--since`-only backfill can cleanly resume from there.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

from server.adapters.antigravity.transcript_reader import (
    DEFAULT_APP_DATA_DIRS,
    TailStateStore,
    discover_transcript_files,
)
from server.adapters.antigravity.worker import process_pending, stats_summary
from server.consolidation.store import ConsolidationStore
from server.journal.store import SqliteEventStore


def _print_json(obj: object) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def _parse_iso(value: str) -> datetime:
    """Parse an ISO8601 cutoff; a value with no explicit offset (e.g. a
    plain local-clock-looking "2026-09-24T00:00:00" typed on the command
    line) is assumed UTC, since every timestamp it's compared against
    (event.observed_at, transcript created_at) is UTC-aware -- an
    accidental naive/aware comparison would otherwise raise instead of
    silently misbehaving, which is the right failure mode, but UTC-default
    is friendlier for a one-off CLI cutoff.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _app_data_dirs(args: argparse.Namespace) -> list[Path] | None:
    return [Path(p) for p in args.app_data_dir] if args.app_data_dir else None


def cmd_status(args: argparse.Namespace) -> int:
    dirs = _app_data_dirs(args)
    files = discover_transcript_files(dirs)
    with TailStateStore(args.db) as tail_store:
        known = set(tail_store.all_known_files())
    _print_json(
        {
            "transcript_files_found": len(files),
            "files_previously_tailed": len(known),
            "files_never_tailed": len([f for f in files if str(f.path) not in known]),
            "app_data_dirs": [str(p) for p in (dirs or DEFAULT_APP_DATA_DIRS)],
        }
    )
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    with SqliteEventStore(args.db) as store:
        stats = process_pending(
            store,
            ConsolidationStore(args.db),
            app_data_dirs=_app_data_dirs(args),
            reasoning_auto_accept_threshold=args.auto_accept_threshold,
            run_consolidation=not args.no_consolidation,
        )
    _print_json(stats_summary(stats))
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    if not args.all_app_dirs:
        print("backfill currently only supports --all-app-dirs", file=sys.stderr)
        return 2

    since = _parse_iso(args.since) if args.since else None
    until = _parse_iso(args.until) if args.until else None

    with SqliteEventStore(args.db) as store:
        stats = process_pending(
            store,
            ConsolidationStore(args.db),
            app_data_dirs=_app_data_dirs(args),
            reasoning_auto_accept_threshold=args.auto_accept_threshold,
            run_consolidation=not args.no_consolidation,
            since=since,
            until=until,
        )
    result = stats_summary(stats)
    result["since_used"] = since.isoformat() if since else None
    result["until_used"] = until.isoformat() if until else None
    _print_json(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="antigravity_cli")
    parser.add_argument("--db", type=Path, default=None, help="journal.db path override")
    parser.add_argument(
        "--app-data-dir",
        action="append",
        default=None,
        help="override app_data_dir root(s) (repeatable); default is both known Antigravity install roots",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Show discovered transcripts and tail state")
    p_status.set_defaults(func=cmd_status)

    p_tail = sub.add_parser("tail", help="Tail changed transcripts once and journal new events")
    p_tail.add_argument("--once", action="store_true", help="accepted for CLI-shape compatibility; this command always runs one pass")
    p_tail.add_argument("--auto-accept-threshold", type=float, default=None)
    p_tail.add_argument("--no-consolidation", action="store_true")
    p_tail.set_defaults(func=cmd_tail)

    p_backfill = sub.add_parser("backfill", help="Backfill from existing transcripts")
    p_backfill.add_argument("--all-app-dirs", action="store_true")
    p_backfill.add_argument("--since", type=str, default=None, help="ISO8601 cutoff; drop events before this")
    p_backfill.add_argument("--until", type=str, default=None, help="ISO8601 cutoff; cap tail offset advancement at this point")
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
