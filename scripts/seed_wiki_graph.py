"""MS7b Phase 3a -- seed :Section, :Note, and :Entity nodes into a fresh
FalkorDB graph from the Phase 2 registries (build_wiki_sections.py,
build_wiki_entities.py).

Deliberately does NOT touch episodes. Run scripts/rebuild_graph_from_ledger.py
against this same --graph-name afterward -- episode replay then resolves
its extracted entity mentions onto the :Entity nodes seeded here BY NAME
(Graphiti's own resolution), which is the entire point of seeding first
(see docs/plan-active.md's MS7b section, finding 3).

What gets written:
  - :Note        one per scanned note + one stub per referenced-but-
                  unscanned note path (e.g. under RAW/)
  - :Section     one per heading (including boilerplate ones -- kept for
                  structural completeness and their own wikilinks, but
                  never decomposed into entities)
  - :Entity      one per distinct decomposed entity name, with a real
                  name_embedding via the configured local embedder, so
                  Graphiti's own name-similarity resolution can use them
                  during episode replay -- built with the identical
                  FalkorDB save shape graphiti_core's own EntityNode.save()
                  uses (MERGE ... SET n:Entity SET n = row SET
                  n.name_embedding = vecf32(...)), just batched via UNWIND
                  instead of one round trip per entity.
  - (:Section)-[:IN_NOTE]->(:Note)
  - (:Section)-[:CONTAINS]->(:Section)      parent -> child heading
  - (:Section)-[:REFERENCES]->(:Note)       resolved wikilinks
  - (:Section)-[:MENTIONS]->(:Entity)       from build_wiki_entities.py

Usage:
    uv run python scripts/seed_wiki_graph.py --graph-name mem-fabric-local-wiki
        [--sections PATH] [--entities PATH] [--dry-run] [--batch-size 500]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

DEFAULT_SECTIONS = Path("imports/state/wiki_sections.json")
DEFAULT_ENTITIES = Path("imports/state/wiki_entities.json")
CYPHER_BATCH = 500


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


async def seed(graph_name: str, sections_path: Path, entities_path: Path, batch_size: int, dry_run: bool) -> dict:
    # Import here, after FALKORDB_DATABASE is set by main() -- mirrors
    # rebuild_graph_from_ledger.py's own convention for the same reason.
    from server.providers.memory_graphiti import get_graphiti, _build_embedder  # noqa: F401 (embedder factory)
    from server.core.config import load_config

    sections_reg = json.loads(sections_path.read_text())
    entities_reg = json.loads(entities_path.read_text())
    notes = sections_reg["notes"]
    sections = sections_reg["sections"]
    entities = entities_reg["entities"]

    stats = {
        "graph_name": graph_name,
        "notes": len(notes),
        "sections": len(sections),
        "distinct_entities": len(entities),
        "dry_run": dry_run,
    }

    if dry_run:
        logger.info(f"[dry-run] would seed {len(notes)} notes, {len(sections)} sections "
                    f"({sum(1 for s in sections if s['is_boilerplate'])} boilerplate), "
                    f"{len(entities)} entities into {graph_name!r}")
        return stats

    graphiti = get_graphiti(graph_name=graph_name)
    driver = graphiti.driver

    # ---- Notes ----
    # `name` is set equal to note_path (not just note_path itself) because
    # FalkorDB Browser's node caption falls back to the raw node ID when no
    # `name` property is present -- Entity nodes display fine because
    # graphiti_core's own convention already gives them one; Section/Note
    # didn't, and showed as bare IDs in the Browser until the user set this by
    # hand (2026-09-13). Setting it here makes every future reseed correct
    # without a manual follow-up query.
    note_rows = [{
        "note_id": n["note_id"], "note_path": n["note_path"], "name": n["note_path"],
        "top_level_area": n["top_level_area"], "is_stub": n["is_stub"],
        "source": "wiki",
    } for n in notes]
    for chunk in _chunks(note_rows, batch_size):
        await driver.execute_query(
            "UNWIND $rows AS row MERGE (n:Note {note_id: row.note_id}) SET n = row",
            rows=chunk,
        )
    logger.info(f"Seeded {len(note_rows)} :Note nodes")

    # ---- Sections ----
    # `name` = heading_clean, same Browser-caption reasoning as :Note above.
    section_rows = [{
        "section_id": s["section_id"], "note_path": s["note_path"], "level": s["level"],
        "heading_raw": s["heading_raw"], "heading_clean": s["heading_clean"], "name": s["heading_clean"],
        "lede": s.get("lede", ""), "is_boilerplate": s["is_boilerplate"],
        "body_word_count": s.get("body_word_count", 0), "source": "wiki",
    } for s in sections]
    for chunk in _chunks(section_rows, batch_size):
        await driver.execute_query(
            "UNWIND $rows AS row MERGE (n:Section {section_id: row.section_id}) SET n = row",
            rows=chunk,
        )
    logger.info(f"Seeded {len(section_rows)} :Section nodes")

    # ---- Section -[:IN_NOTE]-> Note ----
    in_note_rows = [{"section_id": s["section_id"], "note_path": s["note_path"]} for s in sections]
    for chunk in _chunks(in_note_rows, batch_size):
        await driver.execute_query(
            "UNWIND $rows AS row "
            "MATCH (s:Section {section_id: row.section_id}), (n:Note {note_path: row.note_path}) "
            "MERGE (s)-[:IN_NOTE]->(n)",
            rows=chunk,
        )
    logger.info(f"Wrote {len(in_note_rows)} IN_NOTE edges")

    # ---- Section -[:CONTAINS]-> Section (parent -> child) ----
    contains_rows = [{"parent_id": s["parent_id"], "child_id": s["section_id"]}
                      for s in sections if s.get("parent_id")]
    for chunk in _chunks(contains_rows, batch_size):
        await driver.execute_query(
            "UNWIND $rows AS row "
            "MATCH (p:Section {section_id: row.parent_id}), (c:Section {section_id: row.child_id}) "
            "MERGE (p)-[:CONTAINS]->(c)",
            rows=chunk,
        )
    logger.info(f"Wrote {len(contains_rows)} CONTAINS edges")

    # ---- Section -[:REFERENCES]-> Note (resolved wikilinks) ----
    ref_rows = [{"section_id": s["section_id"], "note_path": l["resolved_note_path"]}
                for s in sections for l in s.get("wikilinks", []) if l.get("resolved_note_path")]
    for chunk in _chunks(ref_rows, batch_size):
        await driver.execute_query(
            "UNWIND $rows AS row "
            "MATCH (s:Section {section_id: row.section_id}), (n:Note {note_path: row.note_path}) "
            "MERGE (s)-[:REFERENCES]->(n)",
            rows=chunk,
        )
    logger.info(f"Wrote {len(ref_rows)} REFERENCES edges")

    # ---- Entities: batch-embed names, then batch-write nodes ----
    cfg = load_config()
    embedder = _build_embedder(cfg)
    keys = list(entities.keys())
    names = [entities[k]["name"] for k in keys]
    now = datetime.now(timezone.utc).isoformat()

    entity_rows = []
    uuid_by_key: dict[str, str] = {}
    EMBED_BATCH = 64
    for start in range(0, len(names), EMBED_BATCH):
        batch_names = names[start:start + EMBED_BATCH]
        vectors = await embedder.create_batch(batch_names)
        for key, name, vec in zip(keys[start:start + EMBED_BATCH], batch_names, vectors):
            u = str(uuidlib.uuid4())
            uuid_by_key[key] = u
            rec = entities[key]
            entity_rows.append({
                "uuid": u, "name": name, "name_embedding": list(vec),
                # "_" -- graphiti_core's own default group_id for an episode
                # with none explicitly passed (which remember() never does).
                # This was "" until found broken 2026-09-13: Graphiti's own
                # entity-resolution/dedup search is scoped BY group_id, so a
                # seeded entity in a different partition is never even
                # considered a merge candidate during replay -- not a
                # name-matching failure, a partition mismatch. Confirmed via
                # two "Anthropic" nodes, byte-identical name, group_id ""
                # vs "_". Matching it here is what makes episode replay
                # actually resolve onto these seeded nodes by name.
                "group_id": "_", "summary": "", "created_at": now,
                "source": "wiki", "wiki_type": rec.get("type", ""),
                "wiki_mention_count": rec.get("mention_count", 0),
            })
        logger.info(f"  embedded {min(start+EMBED_BATCH, len(names))}/{len(names)} entity names")

    for chunk in _chunks(entity_rows, batch_size):
        await driver.execute_query(
            "UNWIND $rows AS row "
            "MERGE (n:Entity {uuid: row.uuid}) "
            "SET n:Entity "
            "SET n = row "
            "SET n.name_embedding = vecf32(row.name_embedding)",
            rows=chunk,
        )
    logger.info(f"Seeded {len(entity_rows)} :Entity nodes")

    # ---- Section -[:MENTIONS]-> Entity ----
    mention_rows = [{"section_id": sid, "entity_uuid": uuid_by_key[key]}
                     for key, rec in entities.items() for sid in rec["sections"]]
    for chunk in _chunks(mention_rows, batch_size):
        await driver.execute_query(
            "UNWIND $rows AS row "
            "MATCH (s:Section {section_id: row.section_id}), (e:Entity {uuid: row.entity_uuid}) "
            "MERGE (s)-[:MENTIONS]->(e)",
            rows=chunk,
        )
    logger.info(f"Wrote {len(mention_rows)} Section-MENTIONS->Entity edges")

    stats.update({
        "note_rows": len(note_rows), "section_rows": len(section_rows),
        "in_note_edges": len(in_note_rows), "contains_edges": len(contains_rows),
        "reference_edges": len(ref_rows), "entity_rows": len(entity_rows),
        "mention_edges": len(mention_rows),
    })
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--graph-name", required=True)
    parser.add_argument("--sections", type=Path, default=DEFAULT_SECTIONS)
    parser.add_argument("--entities", type=Path, default=DEFAULT_ENTITIES)
    parser.add_argument("--batch-size", type=int, default=CYPHER_BATCH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    os.environ["FALKORDB_DATABASE"] = args.graph_name

    t0 = time.monotonic()
    stats = asyncio.run(seed(args.graph_name, args.sections, args.entities, args.batch_size, args.dry_run))
    stats["elapsed_seconds"] = round(time.monotonic() - t0, 1)
    print(json.dumps(stats, indent=1))


if __name__ == "__main__":
    main()
