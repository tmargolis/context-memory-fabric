"""Claude Desktop Cowork adapter CLI.

    python -m server.adapters.claude_cowork.cli status
    python -m server.adapters.claude_cowork.cli tail [--max-conversations 25]
    python -m server.adapters.claude_cowork.cli backfill [--since ISO8601] [--max-conversations 25] [--no-consolidation]
    python -m server.adapters.claude_cowork.cli extract [--max-conversations 25] [--order yield|density|oldest] [--dry-run]

`tail` is what the launchd poller runs. `backfill` is the same pass with an
optional cutoff; run it repeatedly to work through the history a batch of
`--max-conversations` extract-eligible conversations at a time (journal-only
scheduled sessions are never capped). `extract` consolidates conversations
that are already journaled but were never extracted -- after a
`--no-consolidation` backfill, offsets have moved past them, so `tail`
won't revisit them. Both skip the whole pass, touching
nothing, if another Spark job holds imports/journal/spark_job.lock or the
MS9 Phase 4 wiki extraction is running.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path

from server.adapters.claude_code.transcript_reader import TailStateStore
from server.adapters.claude_cowork.discovery import discover_transcripts, scheduled_allowlist
from server.adapters.claude_cowork.worker import (
    TAIL_TABLE,
    extract_pending,
    pending_extract_conversations,
    process_pending,
    stats_summary,
)
from server.consolidation.store import ConsolidationStore
from server.journal.store import SqliteEventStore

DEFAULT_MAX_CONVERSATIONS = 25


def _print_json(obj: object) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def cmd_status(args: argparse.Namespace) -> int:
    transcripts = discover_transcripts(args.sessions_root)
    allow = scheduled_allowlist()
    with TailStateStore(args.db, table=TAIL_TABLE) as tail_store:
        known = set(tail_store.all_known_files())
    _print_json({
        "transcripts_found": len(transcripts),
        "never_tailed": sum(1 for t in transcripts if t.tail_key not in known),
        "by_session_type": dict(Counter(t.session_type for t in transcripts)),
        "extract_eligible": sum(1 for t in transcripts if t.extract_eligible(allow)),
        "scheduled_tasks_opted_in": sorted(allow),
        "cloud_handoffs": sum(1 for t in transcripts if t.sidecar.get("outboundCCRRemoteId")),
        "projects": dict(Counter(t.project or "(none)" for t in transcripts).most_common(12)),
    })
    return 0


def _run(args: argparse.Namespace, since: datetime | None) -> int:
    with SqliteEventStore(args.db) as store:
        stats = process_pending(
            store,
            None if args.no_consolidation else ConsolidationStore(args.db),
            sessions_root=args.sessions_root,
            run_consolidation=not args.no_consolidation,
            since=since,
            max_conversations=args.max_conversations,
            reasoning_auto_accept_threshold=args.auto_accept_threshold,
        )
    summary = stats_summary(stats)
    summary["cutoff_used"] = since.isoformat() if since else None
    _print_json(summary)
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    return _run(args, None)


def cmd_backfill(args: argparse.Namespace) -> int:
    since = datetime.fromisoformat(args.since.replace("Z", "+00:00")) if args.since else None
    return _run(args, since)


def cmd_extract(args: argparse.Namespace) -> int:
    with SqliteEventStore(args.db) as store:
        if args.dry_run:
            if args.retriage:
                from server.adapters.claude_cowork.worker import triaged_scheduled_conversations
                pending = triaged_scheduled_conversations(store)
            elif args.stubs:
                from server.adapters.claude_cowork.worker import _reasoning_floor, pending_extract_ranked
                floor = _reasoning_floor()
                pending = [r.conversation_id for r in pending_extract_ranked(store, args.order) if r.events < floor]
            else:
                pending = pending_extract_conversations(store, order=args.order)
            _print_json({"pending_extract_conversations": len(pending), "next_batch": pending[: args.max_conversations]})
            return 0
        stats = extract_pending(store, ConsolidationStore(args.db), max_conversations=args.max_conversations,
                                reasoning_auto_accept_threshold=args.auto_accept_threshold, order=args.order,
                                stubs_only=args.stubs, retriage=args.retriage)
    summary = stats_summary(stats)
    summary["conversations"] = stats.extract_conversations
    _print_json(summary)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude_cowork_cli")
    parser.add_argument("--db", type=Path, default=None, help="journal.db path override")
    parser.add_argument("--sessions-root", type=Path, default=None, help="local-agent-mode-sessions override (testing)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Discovered Cowork transcripts and tail state").set_defaults(func=cmd_status)
    for name, func, helptext in (("tail", cmd_tail, "One poll: journal new lines, consolidate eligible conversations"),
                                 ("backfill", cmd_backfill, "Like tail, with an optional --since cutoff")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--max-conversations", type=int, default=DEFAULT_MAX_CONVERSATIONS,
                       help=f"cap on extract-eligible conversations per pass (default {DEFAULT_MAX_CONVERSATIONS})")
        p.add_argument("--no-consolidation", action="store_true", help="journal only; no Spark, no lock")
        p.add_argument("--auto-accept-threshold", type=float, default=None)
        if name == "backfill":
            p.add_argument("--since", type=str, default=None, help="ISO8601 cutoff")
        p.set_defaults(func=func)
    p = sub.add_parser("extract", help="Consolidate already-journaled, not-yet-extracted eligible conversations")
    p.add_argument("--max-conversations", type=int, default=DEFAULT_MAX_CONVERSATIONS)
    p.add_argument("--auto-accept-threshold", type=float, default=None)
    p.add_argument("--dry-run", action="store_true", help="list what the next batch would be")
    p.add_argument("--retriage", action="store_true",
                   help="re-send windows triage withheld ('no user turns') in allowlisted scheduled-task sessions, triage off")
    p.add_argument("--stubs", action="store_true",
                   help="only the 1-2-event conversations triage normally withholds; sends them to the model anyway")
    p.add_argument("--order", choices=["yield", "density", "oldest"], default="yield",
                   help="yield (default): assistant prose per event, highest first; density: typed-turn share; oldest: by first event")
    p.set_defaults(func=cmd_extract)
    return parser


def main(argv: list[str] | None = None) -> int:
    # args.db stays None by default, as in the claude_code CLI: every store
    # treats None as the production journal, and ConsolidationStore(None) is
    # what routes episode mirrors to the real project-root episode-proposals/
    # (a concrete db path puts them next to the db -- the 2026-09-23 bug).
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
