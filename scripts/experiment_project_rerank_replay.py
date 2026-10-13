"""Ceiling test for B08's project-aware ranking question (2026-10-13), read-only.

Neither candidate signal from the B08 writeup is live in production -- there is
no query-time "which project is this about" inference, and recall_mem returns
ranked facts with no exposed numeric score (server/providers/memory_graphiti.py
`_rrf_merge` keeps the RRF score internal), so a "soft boost" can only be
approximated here as a full reorder (every pool fact matching the target
project moved ahead of the rest, original order preserved within each group),
not a true additive nudge. Before building that query-time inference or
touching recall_mem's return shape, this checks whether the idea has a ceiling
worth building at all, using an *oracle* target project (the case's own gold
episodes' project -- not obtainable at real query time, but the best-case
stand-in for "a classifier got it right").

For each of the 66 ms7_eval cases (`tests/fixtures/ms7_eval/queries.json`,
gold resolved the same way scripts/replay_eval.py does): pull a 24-wide
candidate pool from the live graph via recall_mem, then compare three
re-rankings of that *same* pool, each truncated to k=8 and graded with the
production grade_case/summarize:

  baseline   pool[:8], unchanged -- what recall_mem already returns
  boost      pool reordered: facts whose episode's project matches the
             case's oracle target project moved ahead of the rest
  cap        pool walked in original rank order; a fact is held back once
             its own episode's project already holds CAP slots, overflow
             fills any remaining slots afterward in original order (needs
             no oracle target -- purely structural, closest to what a real
             deployment could do without query-time project inference)

No production code path is touched -- recall_mem is called exactly as
replay_eval.py already calls it; the reordering happens only in this script,
on its own copy of the returned list.

    uv run python scripts/experiment_project_rerank_replay.py --cap 4 --pool 24
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from server.replay.grading import grade_case, summarize  # noqa: E402
from server.replay.runner import resolve_gold  # noqa: E402
from server.replay.snapshot import graph_episode_rows, memory_names  # noqa: E402

GRAPH = "mem-fabric-local"
CASES_PATH = _ROOT / "tests" / "fixtures" / "ms7_eval" / "queries.json"


def episode_projects(graph: str) -> dict[str, str]:
    from server.core.falkordb_conn import falkordb_client
    g = falkordb_client().select_graph(graph)
    rows = g.ro_query("MATCH (e:Episodic) WHERE e.project IS NOT NULL RETURN e.name, e.project").result_set
    return {name: project for name, project in rows}


def oracle_project(case: dict, proj_of: dict[str, str]) -> str | None:
    projects = [proj_of[e] for e in case.get("gold_episodes") or [] if e in proj_of]
    return Counter(projects).most_common(1)[0][0] if projects else None


def fact_project(fact: dict, proj_of: dict[str, str]) -> str | None:
    for name in fact.get("episode_names") or []:
        if name in proj_of:
            return proj_of[name]
    return None


def boost(pool: list[dict], target: str | None, proj_of: dict[str, str], k: int) -> list[dict]:
    if target is None:
        return pool[:k]
    matched = [f for f in pool if fact_project(f, proj_of) == target]
    rest = [f for f in pool if fact_project(f, proj_of) != target]
    return (matched + rest)[:k]


def cap(pool: list[dict], limit: int, proj_of: dict[str, str], k: int) -> list[dict]:
    kept, overflow, counts = [], [], Counter()
    for f in pool:
        p = fact_project(f, proj_of)
        if p is None or counts[p] < limit:
            kept.append(f)
            if p is not None:
                counts[p] += 1
        else:
            overflow.append(f)
        if len(kept) >= k:
            break
    for f in overflow:
        if len(kept) >= k:
            break
        kept.append(f)
    return kept[:k]


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--pool", type=int, default=24, help="candidate pool width pulled from recall_mem per case")
    ap.add_argument("--cap", type=int, default=4, help="max facts per project for the cap variant")
    ap.add_argument("--out", default=str(_ROOT / "imports" / "replay" / "b08-project-rerank-ceiling-20261013.json"))
    args = ap.parse_args()

    from server.providers.memory_graphiti import close_graphiti, recall_mem

    cases_raw = json.loads(CASES_PATH.read_text())["queries"]
    rows = graph_episode_rows(GRAPH)
    cases, problems = resolve_gold(cases_raw, memory_names(rows))
    if problems:
        print(f"gold problems: {len(problems)} (continuing)", file=sys.stderr)

    proj_of = episode_projects(GRAPH)
    print(f"episode->project map: {len(proj_of)} episodes")

    grades = {"baseline": [], "boost": [], "cap": []}
    oracle_hits = 0
    for case in cases:
        pool = await recall_mem(case["query"], max_results=args.pool, format_for_mcp=False)
        target = oracle_project(case, proj_of)
        oracle_hits += target is not None
        variants = {
            "baseline": pool[:args.k],
            "boost": boost(pool, target, proj_of, args.k),
            "cap": cap(pool, args.cap, proj_of, args.k),
        }
        for name, facts in variants.items():
            grades[name].append(grade_case(case, "now", name, facts, [], {}, None))
    await close_graphiti()

    print(f"cases with a resolvable oracle target project: {oracle_hits}/{len(cases)}\n")
    summaries = {name: summarize(gs, args.k) for name, gs in grades.items()}
    for name, s in summaries.items():
        print(f"{name:10s} hit@{args.k}={s['memory_hit_at_k']:.4f}  mrr={s['memory_mrr']:.4f}  "
              f"provenance_rate={s['provenance_rate']:.3f}")

    print("\nper-case rank changes (baseline -> boost / cap), only where they differ:")
    by_id = {g.case_id: g for g in grades["baseline"]}
    for b_id, bg in by_id.items():
        boost_g = next(g for g in grades["boost"] if g.case_id == b_id)
        cap_g = next(g for g in grades["cap"] if g.case_id == b_id)
        if (boost_g.memory_rank, cap_g.memory_rank) != (bg.memory_rank, bg.memory_rank):
            print(f"  {b_id}: baseline={bg.memory_rank} boost={boost_g.memory_rank} cap={cap_g.memory_rank}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "summaries": summaries,
        "oracle_hits": oracle_hits, "cases": len(cases),
        "grades": {name: [g.as_dict() for g in gs] for name, gs in grades.items()},
    }, indent=2) + "\n")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
