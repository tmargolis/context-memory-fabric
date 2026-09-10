"""Review CLI — MS6.

    python -m server.review.cli backfill [--apply]
    python -m server.review.cli stats
    python -m server.review.cli queue [--tier 1] [--project openclaw]
    python -m server.review.cli export --out review.json [--tier 1]
    python -m server.review.cli apply --verdicts verdicts.json
    python -m server.review.cli explain <memory_id> [--graph]
    python -m server.review.cli bulk-reject --category ambiguous --reason "..." [--apply]
    python -m server.review.cli retire-stale-versions [--apply]
    python -m server.review.cli confirm-superseded [--apply]
    python -m server.review.cli sample-audit <batch_id> [-n 100]
    python -m server.review.cli revert-batch <batch_id> [--apply]
    python -m server.review.cli expand-evidence <memory_id> --event-ids ID [ID ...] --reason "..." [--apply]
    python -m server.review.cli promote [--apply] [--limit N]
    python -m server.review.cli correct-memory <memory_id> --content "..." --reason "..." [--apply]
    python -m server.review.cli delete-memory <memory_id> --reason "..." [--apply]

The CLI owns the *machine* half of review — building the queue, exporting
it, applying verdicts back, bulk actions, promotion. The *human* half is
the review artifact the export feeds, because reading 36,000 words of
statements in a terminal, with scrollback as the only navigation and a
command round-trip per verdict, is roughly twice the cost per decision.

Every mutating command defaults to a dry run and requires `--apply`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from server.consolidation.promotion import PromotionStore
from server.consolidation.store import ConsolidationStore
from server.journal.store import SqliteEventStore
from server.review import actions, projects
from server.review.explain import explain
from server.policies.reasoning_episode_v1 import REASONING_POLICY_VERSION
from server.review.queue import review_queue
from server.review.store import ReviewStore


def _print_json(obj: object) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def cmd_backfill(args: argparse.Namespace) -> int:
    with ConsolidationStore(args.db) as store:
        result = projects.backfill(store._conn, dry_run=not args.apply)
    _print_json(result)
    if result["dry_run"]:
        print(f"\nDRY RUN — {result['rows_considered']} rows would be classified. Re-run with --apply.",
              file=sys.stderr)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with ConsolidationStore(args.db) as cs, ReviewStore(args.db) as rs, PromotionStore(args.db) as ps:
        q = review_queue(cs._conn, rs, ps, tier=1, policy_version=args.policy_version)
        _print_json(
            {
                "tier1_pending": q["episode_count"],
                "tier1_total": q["tier1_total"],
                "tier2_total": q["tier2_total"],
                "buckets": {b["project"]: b["tier1_count"] for b in q["buckets"]},
                "review_states": rs.counts(),
                "promoted": ps._conn.execute("SELECT COUNT(*) FROM promotions WHERE status='succeeded'").fetchone()[0],
                "derived_by_state": {
                    r["approval_state"]: r["n"]
                    for r in cs._conn.execute(
                        "SELECT approval_state, COUNT(*) n FROM derived_memories GROUP BY 1 ORDER BY n DESC"
                    )
                },
            }
        )
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    with ConsolidationStore(args.db) as cs, ReviewStore(args.db) as rs, PromotionStore(args.db) as ps:
        q = review_queue(
            cs._conn, rs, ps, tier=args.tier, projects=args.project or None, harness=args.harness,
            policy_version=args.policy_version,
        )
        for bucket in q["buckets"]:
            if not bucket["episodes"]:
                continue
            print(f"\n=== {bucket['project']}  ({len(bucket['episodes'])} pending, {bucket['tier2_count']} tier-2) ===")
            for ep in bucket["episodes"]:
                date = (ep["event_date"] or "")[:10]
                print(f"  [{ep['reasoning_kind'][:14]:14s}] {date}  {ep['statement'][:100]}")
        print(f"\n{q['episode_count']} pending across {q['bucket_count']} buckets "
              f"({q['already_reviewed']} already reviewed, {q['tier2_total']} tier-2 not in scope)")
        if q["episode_count"] == 0:
            _hint_other_versions(cs, args.policy_version)
    return 0


def _hint_other_versions(cs: ConsolidationStore, requested: str) -> None:
    """An empty queue means "none at this version", not "nothing to review".

    Worth saying out loud: the default tracks the *current* policy version, so
    a version bump silently empties the queue while a full backlog sits under
    the previous one. Without this the only symptom is a confusing zero.
    """
    rows = cs._conn.execute(
        "SELECT policy_version, COUNT(*) n FROM derived_memories "
        "WHERE policy_name='reasoning-episode' AND approval_state='queued_for_review' "
        "GROUP BY 1 ORDER BY n DESC"
    ).fetchall()
    by_version = {r["policy_version"]: r["n"] for r in rows}
    if by_version.get(requested):
        # Rows exist at this version; the queue is empty because they are
        # already reviewed or out of tier scope. That is a finished backlog,
        # not a missing one — saying "nothing at this version" would be wrong.
        return
    others = [(v, n) for v, n in by_version.items() if v != requested]
    if others:
        listed = ", ".join(f"{v} ({n} queued)" for v, n in others)
        print(f"\nNote: no rows at all at policy version {requested}; other versions: {listed}")
        print(f"      Review them with:  --policy-version {others[0][0]}")


def cmd_export(args: argparse.Namespace) -> int:
    with ConsolidationStore(args.db) as cs, ReviewStore(args.db) as rs, PromotionStore(args.db) as ps:
        q = review_queue(
            cs._conn, rs, ps, tier=args.tier, projects=args.project or None,
            harness=args.harness, include_evidence=True, max_evidence_chars=args.max_evidence_chars,
            policy_version=args.policy_version,
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(q, indent=1, sort_keys=True, default=str))
    size_mb = args.out.stat().st_size / 1_000_000
    print(f"wrote {args.out} — {q['episode_count']} episodes in {q['bucket_count']} buckets, {size_mb:.1f} MB")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    payload = json.loads(args.verdicts.read_text())
    verdicts = payload.get("verdicts", payload) if isinstance(payload, dict) else payload
    with ReviewStore(args.db) as rs:
        _print_json(actions.apply_verdicts(rs, verdicts, reviewer=args.reviewer))
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    with ConsolidationStore(args.db) as cs:
        result = explain(cs._conn, args.memory_id)
    if result is None:
        print(f"No derived memory with memory_id={args.memory_id!r}", file=sys.stderr)
        return 1
    if args.graph:
        from server.providers.memory_graphiti import get_graphiti  # lazy — pulls in Graphiti
        from server.review.graph_explain import explain_graph

        with PromotionStore(args.db) as ps:
            graph_result = asyncio.run(explain_graph(ps, args.memory_id, get_graphiti()))
        if graph_result is None:
            print("Not promoted into the graph — journal-only answer above.", file=sys.stderr)
        result["graph"] = graph_result
    _print_json(result)
    return 0


def cmd_correct_memory(args: argparse.Namespace) -> int:
    from server.providers.memory_graphiti import get_graphiti  # lazy — pulls in Graphiti
    from server.review.correction import correct_memory

    with PromotionStore(args.db) as ps, ReviewStore(args.db) as rs:
        result = asyncio.run(
            correct_memory(
                ps, rs, args.memory_id, args.content, get_graphiti(),
                reviewer=args.reviewer, reason=args.reason, dry_run=not args.apply,
            )
        )
    _print_json(result)
    if result.get("dry_run"):
        print("\nDRY RUN — re-run with --apply to write the correction.", file=sys.stderr)
    return 0 if "error" not in result else 1


def cmd_delete_memory(args: argparse.Namespace) -> int:
    from server.providers.memory_graphiti import get_graphiti  # lazy — pulls in Graphiti
    from server.review.correction import delete_memory

    with PromotionStore(args.db) as ps, ReviewStore(args.db) as rs:
        result = asyncio.run(
            delete_memory(
                ps, rs, args.memory_id, get_graphiti(),
                reviewer=args.reviewer, reason=args.reason, dry_run=not args.apply,
            )
        )
    _print_json(result)
    if result.get("dry_run"):
        print("\nDRY RUN — re-run with --apply to remove it from the graph.", file=sys.stderr)
    return 0 if "error" not in result else 1


def cmd_bulk_reject(args: argparse.Namespace) -> int:
    with ConsolidationStore(args.db) as cs, ReviewStore(args.db) as rs:
        result = actions.bulk_reject(
            cs._conn, rs, reason=args.reason, policy_name=args.policy_name,
            category=args.category, approval_state=args.approval_state,
            before_date=args.before_date, reviewer=args.reviewer, dry_run=not args.apply,
        )
    _print_json(result)
    if result.get("dry_run"):
        print(f"\nDRY RUN — {result['matched']} rows would be rejected. Re-run with --apply.", file=sys.stderr)
    return 0


def cmd_retire_stale(args: argparse.Namespace) -> int:
    with ConsolidationStore(args.db) as cs, ReviewStore(args.db) as rs:
        result = actions.bulk_reject_stale_policy_versions(
            cs._conn, rs, policy_name=args.policy_name, reviewer=args.reviewer, dry_run=not args.apply
        )
    _print_json(result)
    return 0


def cmd_confirm_superseded(args: argparse.Namespace) -> int:
    with ConsolidationStore(args.db) as cs, ReviewStore(args.db) as rs:
        _print_json(actions.bulk_confirm_superseded(cs._conn, rs, reviewer=args.reviewer, dry_run=not args.apply))
    return 0


def cmd_sample_audit(args: argparse.Namespace) -> int:
    with ConsolidationStore(args.db) as cs, ReviewStore(args.db) as rs:
        for row in actions.sample_audit(cs._conn, args.batch_id, rs, n=args.n):
            print(f"  [{(row['event_date'] or '')[:10]}] conf={row['confidence']:.2f} {row['statement'][:120]}")
    return 0


def cmd_expand_evidence(args: argparse.Namespace) -> int:
    with ConsolidationStore(args.db) as cs, ReviewStore(args.db) as rs:
        result = actions.expand_evidence(
            cs._conn, rs, args.memory_id, args.event_ids, reason=args.reason,
            reviewer=args.reviewer, dry_run=not args.apply,
        )
    _print_json(result)
    return 0


def cmd_revert(args: argparse.Namespace) -> int:
    with ConsolidationStore(args.db) as cs, ReviewStore(args.db) as rs:
        _print_json(actions.revert_batch(cs._conn, rs, args.batch_id, reviewer=args.reviewer, dry_run=not args.apply))
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    from server.memory import remember  # imported lazily — pulls in Graphiti

    with ConsolidationStore(args.db) as cs, ReviewStore(args.db) as rs, PromotionStore(args.db) as ps, \
            SqliteEventStore(args.db) as js:
        result = asyncio.run(
            actions.promote_approved(cs, js, ps, rs, remember, dry_run=not args.apply, limit=args.limit,
                                      wait_through_rate_limit=not args.no_wait)
        )
    _print_json(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m server.review.cli", description="Context Memory Fabric review CLI (MS6)"
    )
    parser.add_argument("--db", type=Path, default=None, help="Path to journal.db (defaults to imports/journal/journal.db)")
    parser.add_argument("--reviewer", default=actions.DEFAULT_REVIEWER)
    sub = parser.add_subparsers(dest="command", required=True)

    p_backfill = sub.add_parser("backfill", help="Populate thread_key + project columns")
    p_backfill.add_argument("--apply", action="store_true", help="Actually write (default is a dry run)")
    p_backfill.set_defaults(func=cmd_backfill)

    p_stats = sub.add_parser("stats", help="Review queue and promotion counts")
    p_stats.add_argument("--policy-version", default=REASONING_POLICY_VERSION,
                         help="Reasoning policy version to report (default: current, %(default)s)")
    p_stats.set_defaults(func=cmd_stats)

    p_queue = sub.add_parser("queue", help="Print the review queue by project bucket")
    p_queue.add_argument("--tier", type=int, default=1)
    p_queue.add_argument("--project", action="append", default=[])
    p_queue.add_argument("--harness", default=None)
    p_queue.add_argument("--policy-version", default=REASONING_POLICY_VERSION,
                          help="Reasoning policy version to review (default: current, %(default)s)")
    p_queue.set_defaults(func=cmd_queue)

    p_export = sub.add_parser("export", help="Export the queue with evidence inlined, for the review artifact")
    p_export.add_argument("--out", type=Path, required=True)
    p_export.add_argument("--tier", type=int, default=1)
    p_export.add_argument("--project", action="append", default=[])
    p_export.add_argument("--harness", default=None)
    p_export.add_argument("--max-evidence-chars", dest="max_evidence_chars", type=int, default=1200)
    p_export.add_argument("--policy-version", default=REASONING_POLICY_VERSION,
                          help="Reasoning policy version to review (default: current, %(default)s)")
    p_export.set_defaults(func=cmd_export)

    p_apply = sub.add_parser("apply", help="Apply verdicts back from the review surface")
    p_apply.add_argument("--verdicts", type=Path, required=True)
    p_apply.set_defaults(func=cmd_apply)

    p_explain = sub.add_parser("explain", help="Why does this memory exist?")
    p_explain.add_argument("memory_id")
    p_explain.add_argument("--graph", action="store_true",
                            help="Also walk into Graphiti for extracted entities/edges (promoted memories only)")
    p_explain.set_defaults(func=cmd_explain)

    p_bulk = sub.add_parser("bulk-reject", help="Reject a filtered population in one recorded action")
    p_bulk.add_argument("--reason", required=True)
    p_bulk.add_argument("--policy-name", dest="policy_name", default="heuristic-pattern")
    p_bulk.add_argument("--category", default=None)
    p_bulk.add_argument("--approval-state", dest="approval_state", default="queued_for_review")
    p_bulk.add_argument("--before-date", dest="before_date", default=None)
    p_bulk.add_argument("--apply", action="store_true", help="Actually write (default is a dry run)")
    p_bulk.set_defaults(func=cmd_bulk_reject)

    p_stale = sub.add_parser(
        "retire-stale-versions", help="Retire queued rows a newer policy version already re-judged"
    )
    p_stale.add_argument("--policy-name", dest="policy_name", default="heuristic-pattern")
    p_stale.add_argument("--apply", action="store_true")
    p_stale.set_defaults(func=cmd_retire_stale)

    p_conf = sub.add_parser("confirm-superseded", help="Bulk-confirm superseded_by_reasoning rows")
    p_conf.add_argument("--apply", action="store_true")
    p_conf.set_defaults(func=cmd_confirm_superseded)

    p_sa = sub.add_parser("sample-audit", help="Random sample from a bulk batch, for the confirming audit")
    p_sa.add_argument("batch_id")
    p_sa.add_argument("-n", type=int, default=100)
    p_sa.set_defaults(func=cmd_sample_audit)

    p_ee = sub.add_parser("expand-evidence", help="Widen an episode's evidence with connecting journal turns")
    p_ee.add_argument("memory_id")
    p_ee.add_argument("--event-ids", nargs="+", required=True, dest="event_ids")
    p_ee.add_argument("--reason", required=True)
    p_ee.add_argument("--apply", action="store_true")
    p_ee.set_defaults(func=cmd_expand_evidence)

    p_rev = sub.add_parser("revert-batch", help="Undo a bulk action, restoring the prior approval_state")
    p_rev.add_argument("batch_id")
    p_rev.add_argument("--apply", action="store_true")
    p_rev.set_defaults(func=cmd_revert)

    p_prom = sub.add_parser("promote", help="Promote approved episodes into the graph")
    p_prom.add_argument("--apply", action="store_true")
    p_prom.add_argument("--limit", type=int, default=None)
    p_prom.add_argument("--no-wait", action="store_true",
                         help="Stop immediately on a rate-limit wall instead of sleeping through it (old behavior)")
    p_prom.set_defaults(func=cmd_promote)

    p_correct = sub.add_parser("correct-memory", help="Re-issue a promoted episode's content, preserving reference_time")
    p_correct.add_argument("memory_id")
    p_correct.add_argument("--content", required=True, help="Corrected episode body")
    p_correct.add_argument("--reason", required=True)
    p_correct.add_argument("--apply", action="store_true")
    p_correct.set_defaults(func=cmd_correct_memory)

    p_delete = sub.add_parser("delete-memory", help="Remove a promoted episode from the graph (journal untouched)")
    p_delete.add_argument("memory_id")
    p_delete.add_argument("--reason", required=True)
    p_delete.add_argument("--apply", action="store_true")
    p_delete.set_defaults(func=cmd_delete_memory)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
