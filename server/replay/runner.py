"""Run replay cases across snapshots and policies (MS8).

For each snapshot (a point-in-time graph + wiki root, or "now" = production
read-only) and each retrieval policy, every case's question goes through
recall_mem against that graph and a lexical wiki search against that wiki
root, then gets graded. Output is one JSON report plus a trajectories JSONL.

Trajectories carry case ids, ranks, episode names, wiki paths and grades, but
no retrieved text unless `include_text=True`: exported cases stay free of
private source data by default (docs/plan-active.md MS8).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from server.replay.grading import CaseGrade, grade_case, summarize
from server.replay.policies import RetrievalPolicy, applied
from server.replay.snapshot import (
    PRODUCTION_LINEAGE,
    availability,
    first_promotion_by_memory,
    graph_episode_rows,
    memory_names,
)


@dataclass
class Snapshot:
    label: str
    graph: str
    wiki_root: Optional[Path]
    as_of_iso: Optional[str]  # None = "now"
    wiki_uncertain_paths: frozenset[str] = frozenset()
    wiki_paths: Optional[frozenset[str]] = None  # files in the wiki export; None = live wiki


@contextmanager
def falkordb_query_timeout(ms: Optional[int]) -> Iterator[None]:
    """Give every FalkorDB query in this process a per-query timeout (ms).

    Graphiti's FalkorDB driver sends none, so its queries inherit the server's
    TIMEOUT, which drifts: docker-compose sets 30000, but a container created
    before that change starts with 1000 and a restart drops the live fix. A
    replay should not measure that drift, so the runner pins the documented
    value for its own queries only. None or 0 leaves the server default.
    """
    if not ms:
        yield
        return
    from falkordb.asyncio.graph import AsyncGraph

    original = AsyncGraph.query

    async def query(self, q, params=None, timeout=None):  # type: ignore[no-untyped-def]
        return await original(self, q, params=params, timeout=ms if timeout is None else timeout)

    AsyncGraph.query = query  # type: ignore[method-assign]
    try:
        yield
    finally:
        AsyncGraph.query = original  # type: ignore[method-assign]


class _WikiIndex:
    """One scan per wiki root, reused across cases and policies."""

    def __init__(self, root: Optional[Path]):
        self.engine = None
        if root is not None:
            from server.providers.wiki.scanner import CorpusScanner, CorpusSearchEngine
            self.engine = CorpusSearchEngine(CorpusScanner(root_path=root).scan(extract_content=True))

    def search(self, query: str, k: int) -> list[Any]:
        return self.engine.search(query=query, max_results=k) if self.engine else []


def resolve_gold(
    cases: list[dict[str, Any]],
    names_by_memory: dict[str, str],
    gold_updates: Optional[dict[str, list[dict[str, Any]]]] = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Cases with their gold expressed as the graph's episode names.

    `gold_memory_ids` win over `gold_episodes`: memory ids are stable, while
    episode names are renumbered when a graph is rebuilt (18 of the MS7 eval's
    36 gold names pointed at other episodes after the 2026-09-13 rebuild).
    `gold_updates` (case id -> [{"from", "add_memory_ids", "retire_memory_ids",
    "add", "retire"}]) become each case's `gold_updates` in names, for
    grading.effective_gold. Returns (cases, problems)."""
    def names(ids: Iterable[str]) -> list[str]:
        return [names_by_memory[m] for m in ids if m in names_by_memory]

    out: list[dict[str, Any]] = []
    problems: list[str] = []
    for case in cases:
        c = dict(case)
        mids = case.get("gold_memory_ids") or []
        if mids:
            missing = [m for m in mids if m not in names_by_memory]
            if missing:
                problems.append(f"{case['id']}: {len(missing)} gold memory id(s) not in the graph")
            c["gold_episodes"] = names(mids)
        ups = [
            {"from": u.get("from"),
             "add": names(u.get("add_memory_ids") or []) + list(u.get("add") or []),
             "retire": names(u.get("retire_memory_ids") or []) + list(u.get("retire") or [])}
            for u in (gold_updates or {}).get(case["id"], [])
        ]
        if ups:
            c["gold_updates"] = ups
        out.append(c)
    return out, problems


async def run_replay(
    cases: list[dict[str, Any]],
    snapshots: list[Snapshot],
    policies: list[RetrievalPolicy],
    journal_db: Path,
    source_graph: str,
    k: int = 8,
    include_text: bool = False,
    lineage: Iterable[str] = PRODUCTION_LINEAGE,
    gold_updates: Optional[dict[str, list[dict[str, Any]]]] = None,
    episode_rows: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """`gold_updates` (proposed, not yet in the case file) adds a second grading
    per run: `grades_with_updates` / `summary_with_updates`."""
    from server.providers.memory_graphiti import close_graphiti, recall_mem

    rows = episode_rows if episode_rows is not None else graph_episode_rows(source_graph)
    promoted_at = availability(rows, first_promotion_by_memory(journal_db, lineage))
    strict, problems = resolve_gold(cases, memory_names(rows))
    updated = resolve_gold(cases, memory_names(rows), gold_updates)[0] if gold_updates else None
    report: dict[str, Any] = {"k": k, "runs": [], "trajectories": [], "gold_problems": problems}
    saved_db = os.environ.get("FALKORDB_DATABASE")
    try:
        for snap in snapshots:
            wiki = _WikiIndex(snap.wiki_root)
            # Wiki ranking is policy-independent. Cache even empty results,
            # but never reuse them across historical snapshots (or runs).
            wiki_paths_by_query: dict[str, list[str]] = {}
            os.environ["FALKORDB_DATABASE"] = snap.graph
            for policy in policies:
                grades: list[CaseGrade] = []
                grades_up: list[CaseGrade] = []
                with applied(policy):
                    for i, case in enumerate(strict):
                        facts = await recall_mem(case["query"], max_results=k, format_for_mcp=False)
                        query = case["query"]
                        if query not in wiki_paths_by_query:
                            wiki_paths_by_query[query] = [w.relative_path for w in wiki.search(query, k)]
                        paths = wiki_paths_by_query[query]
                        g = grade_case(case, snap.label, policy.name, facts, paths, promoted_at,
                                       snap.as_of_iso, snap.wiki_uncertain_paths, snap.wiki_paths)
                        grades.append(g)
                        g_up = None
                        if updated is not None:
                            g_up = grade_case(updated[i], snap.label, policy.name, facts, paths, promoted_at,
                                              snap.as_of_iso, snap.wiki_uncertain_paths, snap.wiki_paths)
                            grades_up.append(g_up)
                        report["trajectories"].append({
                            "case_id": case["id"], "snapshot": snap.label, "graph": snap.graph,
                            "as_of": snap.as_of_iso, "policy": policy.name,
                            "memory": [
                                {"rank": j, "episode_names": f.get("episode_names") or [],
                                 "superseded": bool(f.get("invalid_at")),
                                 **({"fact": f.get("fact")} if include_text else {})}
                                for j, f in enumerate(facts, 1)
                            ],
                            "wiki": [{"rank": j, "path": p} for j, p in enumerate(paths, 1)],
                            "grade": g.as_dict(),
                            **({"grade_with_updates": g_up.as_dict()} if g_up else {}),
                        })
                await close_graphiti()
                run = {
                    "snapshot": snap.label, "graph": snap.graph, "as_of": snap.as_of_iso,
                    "policy": policy.name, "summary": summarize(grades, k),
                    "grades": [g.as_dict() for g in grades],
                }
                if updated is not None:
                    run["summary_with_updates"] = summarize(grades_up, k)
                    run["grades_with_updates"] = [g.as_dict() for g in grades_up]
                report["runs"].append(run)
                print(f"replay finished: snapshot={snap.label} policy={policy.name} "
                      f"cases={len(grades)}", flush=True)
    finally:
        if saved_db is None:
            os.environ.pop("FALKORDB_DATABASE", None)
        else:
            os.environ["FALKORDB_DATABASE"] = saved_db
    return report


def write_report(report: dict[str, Any], out: Path) -> tuple[Path, Path]:
    out.parent.mkdir(parents=True, exist_ok=True)
    trajectories = out.with_suffix(".trajectories.jsonl")
    with trajectories.open("w") as f:
        for t in report["trajectories"]:
            f.write(json.dumps(t) + "\n")
    body = {k: v for k, v in report.items() if k != "trajectories"}
    out.write_text(json.dumps(body, indent=2) + "\n")
    return out, trajectories
