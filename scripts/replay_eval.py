"""Replay historical questions against point-in-time context and compare policies (MS8).

    uv run python scripts/replay_eval.py \\
        --as-of 2026-09-09T16:00:00+00:00 --as-of 2026-09-10T00:00:00+00:00 --now \\
        --out imports/replay/ms8-report.json

For each --as-of: extracts the LLM_Wiki git repo as of that moment into a
scratch folder, then builds a replay-* snapshot of the source graph as it
stood then (server.replay.snapshot): episodes whose memory had not reached a
--lineage graph yet are removed, facts they invalidated are made current
again, and wiki notes whose file did not exist yet are dropped. The report
records each snapshot's wiki commit window; gold wiki files changed inside it
are flagged as uncertain. --now adds the live production graph and wiki,
read-only. Every case in --cases (default: the MS7 eval set) is then run under
every --policy (default: edge-only and edge+episode-vector) and graded.

Production is never written: the source graph's node and edge counts are
recorded before and after and must match, or the script exits non-zero.
Snapshots are dropped at the end unless --keep-snapshots.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from server.replay.policies import ALL_POLICIES, BUILTIN_POLICIES  # noqa: E402
from server.replay.runner import Snapshot, falkordb_query_timeout, run_replay, write_report  # noqa: E402
from server.replay.snapshot import (  # noqa: E402
    PRODUCTION_LINEAGE,
    drop_snapshot,
    export_wiki_at,
    exported_paths,
    snapshot_graph,
    wiki_window,
)


def _counts(graph: str) -> tuple[int, int]:
    import redis
    r = redis.Redis()
    nodes = r.execute_command("GRAPH.RO_QUERY", graph, "MATCH (n) RETURN count(n)")[1][0][0]
    edges = r.execute_command("GRAPH.RO_QUERY", graph, "MATCH ()-[e]->() RETURN count(e)")[1][0][0]
    return int(nodes), int(edges)


async def _run(args: argparse.Namespace) -> int:
    from dotenv import load_dotenv
    from server.journal.store import DEFAULT_JOURNAL_PATH

    load_dotenv()
    cases = json.loads(Path(args.cases).read_text())
    cases = cases["queries"] if isinstance(cases, dict) else cases
    if args.limit:
        cases = cases[: args.limit]
    policies = [ALL_POLICIES[p] for p in (args.policy or list(BUILTIN_POLICIES))]
    gold_updates = json.loads(Path(args.gold_updates).read_text())["cases"] if args.gold_updates else None
    wiki_repo = Path(os.environ["LLM_WIKI_PATH"]).expanduser() if os.getenv("LLM_WIKI_PATH") else None
    work = Path(args.work_dir or tempfile.mkdtemp(prefix="cmf-replay-"))
    lineage = tuple(g for g in args.lineage.split(",") if g)

    before = _counts(args.source)
    snapshots: list[Snapshot] = []
    built: list[str] = []
    snapshot_meta = []
    try:
        for raw in args.as_of or []:
            as_of = datetime.fromisoformat(raw)
            target = f"replay-{as_of.strftime('%Y%m%dT%H%M')}"
            wiki_root, window, wiki_paths = None, None, None
            if wiki_repo is not None:
                wiki_root = work / target
                export_wiki_at(wiki_repo, as_of, wiki_root)
                window = wiki_window(wiki_repo, as_of)
                wiki_paths = exported_paths(wiki_root)
            built.append(target)
            result = await snapshot_graph(as_of, target, source=args.source, wiki_paths=wiki_paths, lineage=lineage)
            uncertain = frozenset(window.uncertain_paths) if window else frozenset()
            snapshots.append(Snapshot(target, target, wiki_root, as_of.isoformat(), uncertain,
                                      frozenset(wiki_paths) if wiki_paths is not None else None))
            meta = {k: v for k, v in vars(result).items() if k != "removed_episode_names"}
            snapshot_meta.append({**meta, "wiki": window.as_dict() if window else None})
            print(f"snapshot {target}: {result.episodes_after} episodes ({result.episodes_removed} removed), "
                  f"{result.entities_after} entities, {result.invalidations_restored} invalidations restored, "
                  f"{result.notes_removed} notes removed; wiki commit {window.commit if window else None} "
                  f"(window {window.gap_hours if window else None}h, {len(uncertain)} files uncertain)", flush=True)
        if args.now:
            snapshots.append(Snapshot("now", args.source, wiki_repo, None))

        report = await run_replay(cases, snapshots, policies, DEFAULT_JOURNAL_PATH, args.source,
                                  k=args.k, include_text=args.include_text, lineage=lineage,
                                  gold_updates=gold_updates)
    finally:
        if not args.keep_snapshots:
            for target in built:
                drop_snapshot(target)
    after = _counts(args.source)
    report["snapshots"] = snapshot_meta
    report["query_timeout_ms"] = args.query_timeout_ms
    report["production"] = {"graph": args.source, "before": before, "after": after, "unchanged": before == after}
    out, traj = write_report(report, Path(args.out))
    if report.get("gold_problems"):
        print("gold problems:", "; ".join(report["gold_problems"]))
    for run in report["runs"]:
        s = run["summary"]
        print(f"{run['snapshot']:>22} {run['policy']:>20}  hit@{args.k}={s['memory_hit_at_k']}  "
              f"mrr={s['memory_mrr']}  abstain={s['abstention_correct']}  wiki@{args.k}={s['wiki_hit_at_k']}  "
              f"leaks={s['temporal_leaks']}  superseded={s['superseded_returned']}  "
              f"wiki_uncertain={s['wiki_uncertain_cases']}"
              + (f"  hit@{args.k}+proposed={run['summary_with_updates']['memory_hit_at_k']}"
                 if "summary_with_updates" in run else ""))
    print(f"production {args.source}: before={before} after={after} unchanged={before == after}")
    print(f"wrote {out} and {traj}")
    return 0 if before == after else 3


async def _main(args: argparse.Namespace) -> int:
    with falkordb_query_timeout(args.query_timeout_ms):
        return await _run(args)


def _parse() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default=str(_ROOT / "tests" / "fixtures" / "ms7_eval" / "queries.json"))
    ap.add_argument("--as-of", action="append", help="ISO timestamp; repeatable")
    ap.add_argument("--now", action="store_true", help="also run against the live graph and wiki (read-only)")
    ap.add_argument("--policy", action="append", choices=sorted(ALL_POLICIES),
                    help="repeatable; default: the two production recall shapes")
    ap.add_argument("--gold-updates", default=None,
                    help="proposed gold updates JSON ({'cases': {id: [...]}}); graded alongside the case file's gold")
    ap.add_argument("--source", default="mem-fabric-local")
    ap.add_argument("--lineage", default=",".join(PRODUCTION_LINEAGE),
                    help="comma-separated graphs whose ledger promotions date a memory's availability")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--query-timeout-ms", type=int, default=30000,
                    help="per-query FalkorDB timeout for this run (docker-compose's documented value); 0 = server default")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--keep-snapshots", action="store_true")
    ap.add_argument("--include-text", action="store_true", help="put retrieved fact text in the trajectories (private data)")
    return ap.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(_parse())))
