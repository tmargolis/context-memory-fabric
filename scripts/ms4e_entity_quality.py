"""MS4e — entity-quality report for a graph (read-only).

Scores the entities a set of promoted episodes produced, so extraction
variants replayed into scratch graphs (scripts/rebuild_graph_from_ledger.py
with CMF_EXTRACTION_PROFILE / CMF_EPISODE_BODY_REASONING) can be compared
on the same episodes, and against what production extracted for them.

Episodes are selected through the journal's promotions ledger: the
memory_ids in --memory-ids-file, resolved to the episode names they were
promoted under in --graph. Each episode is bucketed as `jspace` or
`control` by `derived_memories.project` (override with --focus-project).

Per bucket it reports:
- episodes, and empty-extraction rate (episodes with no MENTIONS)
- entities per episode (MENTIONS edges / episodes) and distinct entities
- single-mention rate: distinct entities mentioned by exactly one episode
  graph-wide
- RELATES_TO facts per episode (edges touching those entities, created by
  those episodes)
- regex noise share: distinct entities server.providers.entity_filter.noise_category() flags
- type-label mix (only non-empty for the `typed-recall` profile)
- gold hits (tests/fixtures/ms4e/entity_gold.json): which keep / drop names
  are among the entities these episodes mention

No writes, to the graph or the journal.

Usage:
    uv run python scripts/ms4e_entity_quality.py --graph ms4e-C-typed \\
        --memory-ids-file tests/fixtures/ms4e/sample_memory_ids.txt [--json]
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

DEFAULT_GOLD = _PROJECT_ROOT / "tests" / "fixtures" / "ms4e" / "entity_gold.json"

from server.providers.entity_filter import noise_category  # noqa: E402  (after the sys.path insert)

def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def gold_hits(entity_names: set[str], gold: dict[str, list[str]]) -> dict[str, dict[str, Any]]:
    present = {norm(n) for n in entity_names}
    out = {}
    for key in ("keep", "drop_prompt", "drop_heldout"):
        names = gold.get(key, [])
        found = [g for g in names if norm(g) in present]
        out[key] = {"total": len(names), "found": len(found), "names": found}
    return out


def _records(rows: Any) -> list[Any]:
    return rows[0] if rows and isinstance(rows[0], list) else (rows or [])


def _read_memory_ids(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")]


def _episode_buckets(db_path: Path, graph: str, memory_ids: list[str], focus: str) -> dict[str, str]:
    """episode_name -> 'jspace'/'control' (focus project vs everything else)."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        qmarks = ",".join("?" * len(memory_ids))
        rows = conn.execute(
            "SELECT p.episode_name, d.project FROM promotions p "
            "JOIN derived_memories d ON d.memory_id = p.memory_id "
            f"WHERE p.graph_name = ? AND p.status = 'succeeded' AND p.memory_id IN ({qmarks})",
            (graph, *memory_ids),
        ).fetchall()
    finally:
        conn.close()
    return {r["episode_name"]: ("focus" if r["project"] == focus else "control") for r in rows}


async def _query(driver: Any, cypher: str, **params: Any) -> list[Any]:
    return _records(await driver.execute_query(cypher, **params))


async def score_graph(graph: str, memory_ids: list[str], db_path: Path, gold: dict, focus: str) -> dict[str, Any]:
    from server.providers.memory_graphiti import close_graphiti, get_graphiti

    buckets = _episode_buckets(db_path, graph, memory_ids, focus)
    driver = get_graphiti(graph_name=graph).driver
    try:
        rows = await _query(
            driver,
            "MATCH (ep:Episodic) WHERE ep.name IN $names "
            "OPTIONAL MATCH (ep)-[:MENTIONS]->(e:Entity) "
            "OPTIONAL MATCH (e)<-[:MENTIONS]-(other:Episodic) "
            "WITH ep, e, count(DISTINCT other) AS graph_mentions "
            "RETURN ep.name AS episode, ep.uuid AS ep_uuid, e.uuid AS uuid, e.name AS name, "
            "labels(e) AS labels, graph_mentions",
            names=list(buckets),
        )
        fact_rows = await _query(
            driver,
            "MATCH (ep:Episodic) WHERE ep.name IN $names "
            "MATCH ()-[r:RELATES_TO]->() WHERE ep.uuid IN r.episodes "
            "RETURN ep.name AS episode, count(r) AS facts",
            names=list(buckets),
        )
    finally:
        await close_graphiti()

    facts = {r["episode"]: r["facts"] for r in fact_rows}
    seen_episodes = {r["episode"] for r in rows}
    missing = sorted(set(buckets) - seen_episodes)

    report: dict[str, Any] = {"graph": graph, "missing_episodes": missing, "buckets": {}}
    for bucket in ("focus", "control"):
        eps = [e for e, b in buckets.items() if b == bucket and e in seen_episodes]
        brows = [r for r in rows if r["episode"] in eps and r["uuid"] is not None]
        per_episode = Counter(r["episode"] for r in brows)
        entities = {r["uuid"]: r for r in brows}
        noise = Counter(c for r in entities.values() if (c := noise_category(r["name"])))
        types = Counter(
            next((lab for lab in r["labels"] if lab != "Entity"), "Entity") for r in entities.values()
        )
        n_eps = len(eps) or 1
        n_ent = len(entities) or 1
        report["buckets"][bucket] = {
            "episodes": len(eps),
            "empty_extraction_rate": sum(1 for e in eps if per_episode[e] == 0) / n_eps,
            "entities_per_episode": len(brows) / n_eps,
            "distinct_entities": len(entities),
            "single_mention_rate": sum(1 for r in entities.values() if r["graph_mentions"] == 1) / n_ent,
            "facts_per_episode": sum(facts.get(e, 0) for e in eps) / n_eps,
            "noise_share": sum(noise.values()) / n_ent,
            "noise_by_category": dict(noise.most_common()),
            "noise_examples": sorted(r["name"] for r in entities.values() if noise_category(r["name"]))[:25],
            "type_mix": dict(types.most_common()),
            "entity_names": sorted(r["name"] for r in entities.values()),
        }
    # Only the sample's own entities: a production graph holds far more than
    # the sample, and scratch graphs hold exactly the sample.
    report["gold"] = gold_hits({r["name"] for r in rows if r["name"]}, gold)
    return report


def _print(report: dict[str, Any], focus: str) -> None:
    print(f"== {report['graph']} ==")
    if report["missing_episodes"]:
        print(f"  missing episodes: {len(report['missing_episodes'])} (e.g. {report['missing_episodes'][0]})")
    for bucket, b in report["buckets"].items():
        label = focus if bucket == "focus" else "control"
        print(
            f"  [{label}] episodes={b['episodes']}  entities/ep={b['entities_per_episode']:.1f}  "
            f"distinct={b['distinct_entities']}  empty={b['empty_extraction_rate']:.0%}  "
            f"single-mention={b['single_mention_rate']:.0%}  noise={b['noise_share']:.0%}  "
            f"facts/ep={b['facts_per_episode']:.1f}"
        )
        if b["noise_by_category"]:
            print(f"      noise: {b['noise_by_category']}")
        if len(b["type_mix"]) > 1:
            print(f"      types: {b['type_mix']}")
    for key, g in report["gold"].items():
        print(f"  gold {key}: {g['found']}/{g['total']} present  {g['names']}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--graph", action="append", required=True, help="Graph to score (repeatable)")
    parser.add_argument("--memory-ids-file", required=True)
    parser.add_argument("--focus-project", default="jspace")
    parser.add_argument("--gold", default=str(DEFAULT_GOLD))
    parser.add_argument("--db", default=None, help="Journal path (default: the production journal)")
    parser.add_argument("--json", default=None, help="Also write the full report(s) to this JSON file")
    return parser.parse_args()


async def _main(args: argparse.Namespace) -> int:
    from server.journal.store import DEFAULT_JOURNAL_PATH

    db_path = Path(args.db) if args.db else DEFAULT_JOURNAL_PATH
    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    memory_ids = _read_memory_ids(args.memory_ids_file)
    reports = []
    for graph in args.graph:
        report = await score_graph(graph, memory_ids, db_path, gold, args.focus_project)
        _print(report, args.focus_project)
        reports.append(report)
    if args.json:
        Path(args.json).write_text(json.dumps(reports, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(_parse_args())))
