"""Codex adapter command line interface (MS4d).

Usage:
    python -m server.adapters.codex.cli status
    python -m server.adapters.codex.cli preview [--path PATH] [--session-id ID]
    python -m server.adapters.codex.cli tail [--once] [--no-consolidation] [--explicit-path PATH]
    python -m server.adapters.codex.cli backfill --all [--since ISO8601] [--until ISO8601] [--limit N]
    python -m server.adapters.codex.cli pending
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Optional

from server.adapters.codex.hooks import (
    check_sentinel_fired,
    generate_launchd_plist,
    get_hooks_status,
    install_hooks,
    install_sentinel_hooks,
    uninstall_hooks,
)
from server.adapters.codex.transcript_reader import (
    DEFAULT_JOURNAL_PATH,
    TailStateStore,
    discover_transcript_files,
    get_default_sessions_root,
    preview_transcript_file,
)
from server.adapters.codex.worker import process_pending, stats_summary
from server.journal.store import SqliteEventStore


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.sessions_root).expanduser() if args.sessions_root else get_default_sessions_root()
    files = discover_transcript_files(root)
    with TailStateStore(args.db) as tail_store:
        known = set(tail_store.all_known_files())
        cutoff = tail_store.get_initial_cutoff()
        pending = tail_store.get_pending_extractions()

    _print_json({
        "sessions_root": str(root),
        "transcript_files_found": len(files),
        "files_previously_tailed": len(known),
        "files_never_tailed": len([f for f in files if str(f.path) not in known]),
        "initial_capture_cutoff": cutoff.isoformat() if cutoff else None,
        "pending_extractions_count": len(pending),
    })
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    root = Path(args.sessions_root).expanduser() if args.sessions_root else get_default_sessions_root()
    explicit = Path(args.path).expanduser() if args.path else None
    files = discover_transcript_files(root, explicit_path=explicit, session_id_filter=args.session_id)

    if not files:
        print("No matching transcript files found for preview.", file=sys.stderr)
        return 1

    target = files[-1]  # Most recent
    with TailStateStore(args.db) as tail_store:
        result = preview_transcript_file(target.path, tail_store)

    _print_json(result)
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    explicit = Path(args.explicit_path).expanduser() if args.explicit_path else None
    root = Path(args.sessions_root).expanduser() if args.sessions_root else None

    with SqliteEventStore(args.db) as j_store:
        stats = process_pending(
            j_store,
            sessions_root=root,
            explicit_path=explicit,
            session_id_filter=args.session_id,
            run_consolidation=not args.no_consolidation,
            use_lock=not args.no_lock,
        )

    _print_json(stats_summary(stats))
    return 0 if stats.lock_acquired else 1


def cmd_backfill(args: argparse.Namespace) -> int:
    if not args.all:
        print("Backfill requires explicit --all confirmation flag.", file=sys.stderr)
        return 2

    since = _parse_iso(args.since) if args.since else None
    until = _parse_iso(args.until) if args.until else None
    root = Path(args.sessions_root).expanduser() if args.sessions_root else None

    with SqliteEventStore(args.db) as j_store:
        stats = process_pending(
            j_store,
            sessions_root=root,
            since=since,
            until=until,
            limit=args.limit,
            run_consolidation=not args.no_consolidation,
            use_lock=not args.no_lock,
        )

    _print_json(stats_summary(stats))
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    with TailStateStore(args.db) as tail_store:
        pending = tail_store.get_pending_extractions()
    _print_json(pending)
    return 0


def cmd_hooks(args: argparse.Namespace) -> int:
    hooks_path = Path(args.hooks_path).expanduser() if args.hooks_path else None
    action = args.hooks_action

    if action == "status":
        res = get_hooks_status(hooks_path)
        _print_json(res)
        return 0
    elif action == "install":
        events = tuple(args.events.split(",")) if args.events else ("SessionStart", "Stop")
        res = install_hooks(hooks_path=hooks_path, events=events)
        _print_json(res)
        return 0
    elif action == "uninstall":
        res = uninstall_hooks(hooks_path=hooks_path)
        _print_json(res)
        return 0
    elif action == "sentinel":
        sentinel_log = Path(args.sentinel_log).expanduser() if args.sentinel_log else None
        res = install_sentinel_hooks(hooks_path=hooks_path, sentinel_log=sentinel_log)
        _print_json(res)
        return 0
    elif action == "plist":
        content = generate_launchd_plist(interval_seconds=args.interval)
        if args.out:
            out_p = Path(args.out).expanduser()
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text(content, encoding="utf-8")
            _print_json({"written_to": str(out_p)})
        else:
            print(content, end="")
        return 0
    else:
        print(f"Unknown hooks action: {action}", file=sys.stderr)
        return 2


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Codex Transcript Adapter CLI")
    parser.add_argument("--db", type=Path, default=DEFAULT_JOURNAL_PATH, help="Path to journal SQLite db")
    parser.add_argument("--sessions-root", type=str, default=None, help="Root directory for Codex sessions")

    sub = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = sub.add_parser("status", help="Show discovery and tail status")
    p_status.set_defaults(func=cmd_status)

    # preview
    p_preview = sub.add_parser("preview", help="Read-only preview of transcript capture")
    p_preview.add_argument("--path", type=str, default=None, help="Specific transcript file path")
    p_preview.add_argument("--session-id", type=str, default=None, help="Filter by session ID")
    p_preview.set_defaults(func=cmd_preview)

    # tail
    p_tail = sub.add_parser("tail", help="Tail changed transcripts and process pending extractions")
    p_tail.add_argument("--once", action="store_true", help="Run a single pass (default)")
    p_tail.add_argument("--no-consolidation", action="store_true", help="Journal only, skip consolidation")
    p_tail.add_argument("--no-lock", action="store_true", help="Skip worker lock check (testing only)")
    p_tail.add_argument("--explicit-path", type=str, default=None, help="Tail a single explicit transcript")
    p_tail.add_argument("--session-id", type=str, default=None, help="Filter to specific session ID")
    p_tail.set_defaults(func=cmd_tail)

    # backfill
    p_backfill = sub.add_parser("backfill", help="Historical backfill mode (requires --all)")
    p_backfill.add_argument("--all", action="store_true", help="Explicit confirmation for backfill")
    p_backfill.add_argument("--since", type=str, default=None, help="ISO8601 cutoff: ignore older events")
    p_backfill.add_argument("--until", type=str, default=None, help="ISO8601 cutoff: do not advance past")
    p_backfill.add_argument("--limit", type=int, default=None, help="Max sessions to process")
    p_backfill.add_argument("--no-consolidation", action="store_true", help="Journal only, skip consolidation")
    p_backfill.add_argument("--no-lock", action="store_true", help="Skip worker lock check (testing only)")
    p_backfill.set_defaults(func=cmd_backfill)

    # pending
    p_pending = sub.add_parser("pending", help="List pending extraction queue")
    p_pending.set_defaults(func=cmd_pending)

    # hooks
    p_hooks = sub.add_parser("hooks", help="Manage Codex hooks and poller plist")
    p_hooks.add_argument("hooks_action", choices=["status", "install", "uninstall", "sentinel", "plist"], help="Hooks action")
    p_hooks.add_argument("--hooks-path", type=str, default=None, help="Path to hooks.json")
    p_hooks.add_argument("--events", type=str, default=None, help="Comma-separated event names (default: SessionStart,Stop)")
    p_hooks.add_argument("--sentinel-log", type=str, default=None, help="Path to sentinel log")
    p_hooks.add_argument("--interval", type=int, default=900, help="Launchd poller interval in seconds (default: 900)")
    p_hooks.add_argument("--out", type=str, default=None, help="Write plist content to file instead of stdout")
    p_hooks.set_defaults(func=cmd_hooks)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
