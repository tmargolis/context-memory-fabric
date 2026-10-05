"""Journal CLI: stats, inspect, export, replay.

    python -m server.journal.cli stats
    python -m server.journal.cli inspect <event_id>
    python -m server.journal.cli export --out events.jsonl [--harness chatgpt]
    python -m server.journal.cli replay --out replay.jsonl [--harness chatgpt]

`replay` re-serializes matching events through the same canonicalization
compute_content_hash uses (sort_keys, NFC-normalized) and writes them in a
fixed (observed_at, event_id) order — running it twice against an unchanged
journal produces byte-identical output, which is Milestone 2's acceptance
test 2 ("journal replay with a pinned normalization policy is byte-identical
across runs"). It does not yet re-run consolidation (no such pipeline
exists until Milestone 3) — "replay" here means "deterministically
re-emit the evidence," not "re-derive memory from it."
"""

import argparse
import json
from pathlib import Path
import sys

from server.journal.store import SqliteEventStore


def _print_json(obj: object) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def cmd_stats(args: argparse.Namespace) -> int:
    with SqliteEventStore(args.db) as store:
        _print_json(store.stats())
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    with SqliteEventStore(args.db) as store:
        event = store.get(args.event_id)
        if event is None:
            print(f"No event found with event_id={args.event_id!r}", file=sys.stderr)
            return 1
        _print_json(
            {
                "event_id": event.event_id,
                "schema_version": event.schema_version,
                "event_type": event.event_type,
                "source": {
                    "harness": event.source.harness,
                    "conversation_id": event.source.conversation_id,
                    "session_id": event.source.session_id,
                    "turn_id": event.source.turn_id,
                },
                "actor_type": event.actor_type,
                "actor_id": event.actor_id,
                "observed_at": event.observed_at.isoformat(),
                "event_date": event.event_date.isoformat() if event.event_date else None,
                "date_precision": event.date_precision.value,
                "content": event.content,
                "content_hash": event.content_hash,
                "parent_event_ids": event.parent_event_ids,
                "metadata": event.metadata,
            }
        )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    with SqliteEventStore(args.db) as store:
        events = store.query(harness=args.harness, conversation_id=args.conversation_id)
        with open(args.out, "w", encoding="utf-8") as f:
            for event in events:
                f.write(
                    json.dumps(
                        {
                            "event_id": event.event_id,
                            "event_type": event.event_type,
                            "harness": event.source.harness,
                            "conversation_id": event.source.conversation_id,
                            "observed_at": event.observed_at.isoformat(),
                            "event_date": event.event_date.isoformat() if event.event_date else None,
                            "date_precision": event.date_precision.value,
                            "content": event.content,
                            "content_hash": event.content_hash,
                        },
                        sort_keys=True,
                        ensure_ascii=True,
                    )
                    + "\n"
                )
        print(f"Exported {len(events)} events to {args.out}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Deterministic re-serialization for reproducibility checks. See
    module docstring — this is evidence replay, not memory re-derivation.
    """
    with SqliteEventStore(args.db) as store:
        events = store.query(harness=args.harness, conversation_id=args.conversation_id)
        # store.query already orders by observed_at ASC; break ties on
        # event_id so output order is fully deterministic even when two
        # events share an observed_at timestamp.
        ordered = sorted(events, key=lambda e: (e.observed_at.isoformat(), e.event_id))
        with open(args.out, "w", encoding="utf-8") as f:
            for event in ordered:
                f.write(
                    json.dumps(
                        {
                            "event_id": event.event_id,
                            "event_type": event.event_type,
                            "content_hash": event.content_hash,
                            "content": event.content,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    )
                    + "\n"
                )
        print(f"Replayed {len(ordered)} events to {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m server.journal.cli", description="Context Memory Fabric event journal CLI")
    parser.add_argument("--db", type=Path, default=None, help="Path to journal.db (defaults to imports/journal/journal.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stats", help="Print summary counts").set_defaults(func=cmd_stats)

    p_inspect = sub.add_parser("inspect", help="Print one event by event_id")
    p_inspect.add_argument("event_id")
    p_inspect.set_defaults(func=cmd_inspect)

    p_export = sub.add_parser("export", help="Export matching events as JSONL")
    p_export.add_argument("--out", type=Path, required=True)
    p_export.add_argument("--harness", default=None)
    p_export.add_argument("--conversation-id", dest="conversation_id", default=None)
    p_export.set_defaults(func=cmd_export)

    p_replay = sub.add_parser("replay", help="Deterministically re-emit matching events")
    p_replay.add_argument("--out", type=Path, required=True)
    p_replay.add_argument("--harness", default=None)
    p_replay.add_argument("--conversation-id", dest="conversation_id", default=None)
    p_replay.set_defaults(func=cmd_replay)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
