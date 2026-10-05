"""MS6b Task 2 — cross-project entity report.

Diagnostic only, no writes. Lists graph entities whose mentioning episodes
span more than one `project` bucket, with episode count and summary
length, so a human can separate legitimate cross-project entities (real
things that recur across life domains — `macOS`, `rsync`, `Photoshop`) from
genuine sense-collapse (a name that means different things in different
projects, e.g. "Anthropic" the employer vs. "Anthropic" the API vendor),
per Graphiti's name-only entity resolution.

`project` is read off the promoted episode's `derived_memories.project`
column in the journal (joined via `PromotionStore`'s episode_name), not
anything in the graph itself.

Usage:
    uv run python scripts/entity_audit.py [--db PATH] [--graph-name NAME] [--min-projects N]
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sqlite3

from typing import Any, TypedDict

from server.journal.store import DEFAULT_JOURNAL_PATH
from server.providers.memory_graphiti import get_graphiti


class EntityAuditItem(TypedDict):
    uuid: str
    name: str
    episode_count: int
    projects: list[str]
    summary_len: int
    summary: str | None


def _records(rows: Any) -> list[Any]:
    return rows[0] if rows and isinstance(rows[0], list) else (rows or [])


async def entity_audit(
    db_path: Path | str,
    graph_name: str,
    min_projects: int = 2,
) -> list[EntityAuditItem]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    episode_project = {
        r["episode_name"]: r["project"]
        for r in conn.execute(
            "SELECT p.episode_name AS episode_name, dm.project AS project "
            "FROM promotions p JOIN derived_memories dm ON dm.memory_id = p.memory_id "
            "WHERE p.graph_name = ? AND p.status = 'succeeded'",
            (graph_name,),
        )
    }

    graphiti = get_graphiti()
    rows = _records(
        await graphiti.driver.execute_query(
            "MATCH (e:Entity)<-[:MENTIONS]-(ep:Episodic) "
            "RETURN e.uuid AS uuid, e.name AS name, e.summary AS summary, collect(ep.name) AS episodes"
        )
    )

    report: list[EntityAuditItem] = []
    for r in rows:
        episodes = r["episodes"]
        projects = {episode_project.get(ep, "?") for ep in episodes}
        if len(projects) >= min_projects:
            report.append({
                "uuid": r["uuid"],
                "name": r["name"],
                "episode_count": len(episodes),
                "projects": sorted(projects),
                "summary_len": len(r["summary"] or ""),
                "summary": r["summary"],
            })
    report.sort(key=lambda x: (-len(x["projects"]), -x["episode_count"]))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--graph-name", default="mem-fabric-local")
    parser.add_argument("--min-projects", type=int, default=2)
    args = parser.parse_args()

    report = asyncio.run(entity_audit(args.db or DEFAULT_JOURNAL_PATH, args.graph_name, args.min_projects))
    print(f"{len(report)} entities span >= {args.min_projects} projects in {args.graph_name!r}:\n")
    for e in report:
        print(f"{e['episode_count']:3d} eps  {len(e['projects'])} projects  {e['name'][:34]:34s} "
              f"summary={e['summary_len']:4d}ch  {e['projects']}  uuid={e['uuid']}")


if __name__ == "__main__":
    main()
