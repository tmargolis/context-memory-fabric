"""MS9 Phase 4 — Relationship extraction over wiki text (Option B).

Extracts durable entity relationships (RELATES_TO) over wiki text without
creating Episodic nodes, preserving the strict boundary between episodic memory
and durable knowledge.

- Resolves notes' actual created_at / updated_at timestamps (server/providers/wiki/note_dates.py).
- Uses typed-recall extraction profile + debris filter.
- Resolves onto existing entities (including Phase 2 merges).
- Tags every edge with source='wiki', note_path, section_heading, and authoring timestamps.
- Refuses non-fixgraph-* graphs by default for safety.

Usage:
    uv run python scripts/extract_wiki_relationships.py --graph fixgraph-p4 --notes-scope pilot
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import random
import re
import sys
import time
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from server.core.config import load_config
from server.providers.entity_filter import noise_category
from server.providers.extraction_profile import extraction_kwargs, TYPED_RECALL_EXTRACTION
from server.providers.memory_graphiti import get_graphiti_for_operation
from server.providers.wiki.corpus import get_corpus_root
# Shared with seed/sweep, replay and get_context; re-exported here for existing callers.
from server.providers.wiki.note_dates import resolve_note_dates  # noqa: F401

from graphiti_core.nodes import EpisodicNode, EpisodeType, EntityNode
from graphiti_core.edges import EntityEdge
from graphiti_core.utils.maintenance.node_operations import extract_nodes, resolve_extracted_nodes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("extract_wiki_relationships")

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
BOILERPLATE = {
    "sources", "open questions", "summary", "summary tl dr", "tl dr",
    "connections", "related works", "related", "questions", "tasks",
    "status", "recommendations", "recommendations next steps",
}

PILOT_NOTES = [
    # 13 J-Space notes
    "WIKI/projects/J-Space/J-Space non-factual pilot — first runs.md",
    "WIKI/projects/J-Space/J-Space artworks.md",
    "WIKI/projects/J-Space/J-Space open source models.md",
    "WIKI/projects/J-Space/J-Space visualization method and decisions.md",
    "WIKI/projects/J-Space/J-Space findings so far.md",
    "WIKI/projects/J-Space/J-Space studies.md",
    "WIKI/projects/J-Space/Explainability.md",
    "WIKI/projects/J-Space/J space feasibility.md",
    "WIKI/projects/J-Space/GoT in J-Space.md",
    "WIKI/projects/J-Space/J-Space Recommended installation.md",
    "WIKI/projects/J-Space/J-Space aesthetic testing on Mac Air.md",
    "WIKI/projects/J-Space/J-Space non-factual prompts — repo data inventory.md",
    "WIKI/projects/J-Space/J-Space improvements.md",
    # 5 Cityscapes notes
    "WIKI/art-projects/Cityscapes/Cityscape notes.md",
    "WIKI/art-projects/Cityscapes/Cityscape Vibrancy Glow.md",
    "WIKI/art-projects/Cityscapes/Cityscapes.md",
    "WIKI/art-projects/Cityscapes/Cityscape-View the shadows.md",
    "WIKI/art-projects/Cityscapes/Cityscape-Celestial urban sky.md",
]


def parse_markdown_sections(text: str) -> list[dict[str, Any]]:
    """Parse a markdown note into coherent, non-boilerplate sections."""
    sections = []
    lines = text.splitlines()
    cur_heading = "Introduction"
    cur_buf = []

    start_idx = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start_idx = i + 1
                break

    for line in lines[start_idx:]:
        m = HEADING_RE.match(line)
        if m:
            body = "\n".join(cur_buf).strip()
            clean_heading = re.sub(r"^[0-9]+[.\- ]+", "", cur_heading).strip()
            if len(body.split()) >= 15 and clean_heading.lower() not in BOILERPLATE:
                sections.append({
                    "heading": cur_heading,
                    "clean_heading": clean_heading,
                    "text": body,
                    "words": len(body.split()),
                })
            cur_heading = m.group(2).strip()
            cur_buf = []
        else:
            cur_buf.append(line)

    body = "\n".join(cur_buf).strip()
    clean_heading = re.sub(r"^[0-9]+[.\- ]+", "", cur_heading).strip()
    if len(body.split()) >= 15 and clean_heading.lower() not in BOILERPLATE:
        sections.append({
            "heading": cur_heading,
            "clean_heading": clean_heading,
            "text": body,
            "words": len(body.split()),
        })

    return sections


async def process_section(
    graphiti: Any,
    section: dict[str, Any],
    note_path: str,
    note_dates: dict[str, Any],
    project_slug: str,
    dry_run: bool,
    sem: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    """Process a single section: extract entities + edges under Option (b)."""
    async with sem:
        clean_heading = section["clean_heading"]
        sec_text = section["text"]
        ref_dt = note_dates["created_dt"]

        # 1. Prepare kwargs with typed-recall profile
        source_desc = f"wiki_note={note_path} | project={project_slug}"
        kwargs = extraction_kwargs(profile=TYPED_RECALL_EXTRACTION, source_description=source_desc)
        entity_types = kwargs["entity_types"]
        excluded_entity_types = kwargs["excluded_entity_types"]
        custom_instructions = kwargs["custom_extraction_instructions"]

        # In-memory episode node for extraction context (never saved as :Episodic)
        dummy_episode = EpisodicNode(
            name=f"wiki:{Path(note_path).stem}:{clean_heading}",
            group_id="_",
            labels=[],
            source=EpisodeType.text,
            content=f"Heading: {clean_heading}\n\n{sec_text}",
            source_description=source_desc,
            created_at=ref_dt,
            valid_at=ref_dt,
        )

        try:
            # 2. Extract entities
            extracted_nodes, _ = await extract_nodes(
                graphiti.clients,
                dummy_episode,
                [],
                entity_types,
                excluded_entity_types,
                custom_instructions,
            )
        except Exception as e:
            logger.warning(f"Error extracting nodes for {note_path} [{clean_heading}]: {e}")
            return []

        # Filter debris entities
        clean_extracted_nodes = []
        for n in extracted_nodes:
            cat = noise_category(n.name)
            if not cat:
                clean_extracted_nodes.append(n)

        if len(clean_extracted_nodes) < 2:
            # Need at least 2 entities to form a relationship edge
            return []

        try:
            # 3. Resolve nodes against the graph
            nodes, uuid_map, _ = await resolve_extracted_nodes(
                graphiti.clients,
                clean_extracted_nodes,
                dummy_episode,
                [],
                entity_types,
            )
        except Exception as e:
            logger.warning(f"Error resolving nodes for {note_path} [{clean_heading}]: {e}")
            return []

        # Save any genuinely new entities to the graph (if not dry run)
        if not dry_run:
            for n in nodes:
                try:
                    # Check if node already exists in graph
                    res = await graphiti.driver.execute_query(
                        "MATCH (e:Entity {uuid: $uuid}) RETURN count(e) AS c", uuid=n.uuid
                    )
                    exists = (res[0][0]["c"] if res and res[0] else 0) > 0
                    if not exists:
                        await n.save(graphiti.driver)
                except Exception as e:
                    logger.debug(f"Error ensuring node {n.name}: {e}")

        try:
            # 4. Extract and resolve edges
            edge_type_map = {("Entity", "Entity"): []}
            resolved_edges, _, _ = await graphiti._extract_and_resolve_edges(
                dummy_episode,
                clean_extracted_nodes,
                [],
                edge_type_map,
                "_",
                None,
                nodes,
                uuid_map,
                custom_instructions,
            )
        except Exception as e:
            logger.warning(f"Error extracting edges for {note_path} [{clean_heading}]: {e}")
            return []

        records = []
        for edge in resolved_edges:
            # Set authoring timestamps
            edge.valid_at = ref_dt
            edge.created_at = ref_dt
            edge.reference_time = ref_dt
            edge.episodes = []  # No Episodic node reference

            # Set wiki provenance attributes
            edge.attributes = getattr(edge, "attributes", {}) or {}
            edge.attributes["source"] = "wiki"
            edge.attributes["note_path"] = note_path
            edge.attributes["section_heading"] = clean_heading

            record = {
                "uuid": edge.uuid,
                "source_node_uuid": edge.source_node_uuid,
                "target_node_uuid": edge.target_node_uuid,
                "fact": edge.fact,
                "valid_at": note_dates["created_at"],
                "created_at": note_dates["created_at"],
                "note_path": note_path,
                "section_heading": clean_heading,
                "source": "wiki",
            }
            records.append(record)

            if not dry_run:
                try:
                    await edge.save(graphiti.driver)
                except Exception as e:
                    logger.warning(f"Failed to save edge: {edge.fact} ({e})")

        return records


async def update_note_node_dates(graphiti: Any, note_path: str, dates: dict[str, Any]) -> None:
    """Write a note's dates onto its :Note node (shared writer, see note_dates.py)."""
    from server.providers.wiki.note_dates import NOTE_DATE_FIELDS, write_note_dates
    row = {"note_path": note_path, **{k: dates[k] for k in NOTE_DATE_FIELDS}}
    await write_note_dates(graphiti.driver, [row])


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", default="fixgraph-p4", help="Target graph name (default: fixgraph-p4)")
    parser.add_argument("--notes-scope", choices=["pilot", "all"], default="pilot", help="Notes scope: pilot (18 notes) or all")
    parser.add_argument("--limit-sections", type=int, default=None, help="Cap total sections processed")
    parser.add_argument("--concurrency", type=int, default=3, help="Max parallel extraction requests")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to FalkorDB")
    parser.add_argument("--resume", action="store_true", help="Resume from existing ledger file")
    parser.add_argument("--allow-production", action="store_true", help="Allow running on mem-fabric-local")
    parser.add_argument("--update-note-dates", action="store_true", default=True, help="Update Note nodes with dates")
    parser.add_argument("--out-ledger", type=Path, default=Path("imports/fixgraph/wiki_edges_pilot.jsonl"))
    parser.add_argument("--sample-out", type=Path, default=Path("imports/fixgraph/wiki_edges_sample_50.json"))
    args = parser.parse_args()

    if "fixgraph-" not in args.graph and not args.allow_production:
        print(f"Error: Refusing to run on '{args.graph}' without --allow-production. Use a fixgraph-* scratch graph.", file=sys.stderr)
        sys.exit(2)

    os.environ["FALKORDB_DATABASE"] = args.graph
    os.environ["CMF_EXTRACTION_PROFILE"] = "typed-recall"
    os.environ["CMF_ENTITY_DEBRIS_FILTER"] = "1"

    graphiti, model_name = get_graphiti_for_operation()
    wiki_root = get_corpus_root()

    logger.info(f"Target graph: {args.graph} (model: {model_name}, dry_run: {args.dry_run})")
    logger.info(f"Wiki root: {wiki_root}")

    # Determine target notes
    if args.notes_scope == "pilot":
        target_notes = PILOT_NOTES
    else:
        # Read from wiki_sections.json
        sections_file = _ROOT / "imports/state/wiki_sections.json"
        with open(sections_file) as f:
            sec_data = json.load(f)
        target_notes = [n["note_path"] for n in sec_data["notes"] if not n.get("is_stub")]

    logger.info(f"Targeting {len(target_notes)} notes (scope: {args.notes_scope})")

    # Read and parse sections
    all_tasks = []
    sem = asyncio.Semaphore(args.concurrency)

    # Cache note dates and filter existing notes
    note_dates_map = {}
    valid_notes = []
    for np in target_notes:
        full_path = wiki_root / np
        if not full_path.exists():
            continue
        valid_notes.append(np)
        dates = resolve_note_dates(wiki_root, np)
        note_dates_map[np] = dates
        if not args.dry_run and args.update_note_dates:
            await update_note_node_dates(graphiti, np, dates)

    for np in valid_notes:
        full_path = wiki_root / np
        text = full_path.read_text(encoding="utf-8", errors="replace")
        sections = parse_markdown_sections(text)
        project_slug = "jspace" if "J-Space" in np else ("cityscapes" if "Cityscape" in np else "misc")

        for sec in sections:
            all_tasks.append((sec, np, note_dates_map[np], project_slug))

    if args.limit_sections:
        all_tasks = all_tasks[:args.limit_sections]

    logger.info(f"Total non-boilerplate sections to process: {len(all_tasks)}")

    t0 = time.monotonic()
    all_edge_records = []
    edges_per_note: dict[str, int] = {np: 0 for np in target_notes}

    # Processed sections index for resume support
    args.out_ledger.parent.mkdir(parents=True, exist_ok=True)
    processed_section_keys = set()
    if args.resume and args.out_ledger.exists():
        with open(args.out_ledger, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        row = json.loads(line)
                        k = f"{row.get('note_path')}:{row.get('section_heading')}"
                        processed_section_keys.add(k)
                        all_edge_records.append(row)
                        edges_per_note[row["note_path"]] = edges_per_note.get(row["note_path"], 0) + 1
                    except Exception:
                        pass
        logger.info(f"Resume: loaded {len(all_edge_records)} existing edges across {len(processed_section_keys)} processed sections")

    remaining_tasks = [
        t for t in all_tasks
        if f"{t[1]}:{t[0]['clean_heading']}" not in processed_section_keys
    ]
    logger.info(f"Sections to process: {len(remaining_tasks)} (skipped {len(all_tasks) - len(remaining_tasks)} already processed)")

    # Open ledger in append mode for incremental writes
    ledger_file = open(args.out_ledger, "a" if args.resume else "w", encoding="utf-8")

    try:
        # Process sections in concurrent batches
        batch_size = args.concurrency * 2
        for i in range(0, len(remaining_tasks), batch_size):
            batch = remaining_tasks[i : i + batch_size]
            logger.info(f"Processing batch {i + 1}..{min(i + len(batch), len(remaining_tasks))} of {len(remaining_tasks)}...")

            coros = [
                process_section(
                    graphiti,
                    sec,
                    np,
                    dates,
                    pslug,
                    args.dry_run,
                    sem,
                )
                for (sec, np, dates, pslug) in batch
            ]
            results = await asyncio.gather(*coros)
            batch_count = 0
            for r_list in results:
                for rec in r_list:
                    all_edge_records.append(rec)
                    edges_per_note[rec["note_path"]] = edges_per_note.get(rec["note_path"], 0) + 1
                    ledger_file.write(json.dumps(rec) + "\n")
                    batch_count += 1
            ledger_file.flush()
            logger.info(f"  Batch complete: wrote {batch_count} new edges (total so far: {len(all_edge_records)})")
    finally:
        ledger_file.close()

    elapsed = time.monotonic() - t0
    logger.info(f"Extraction complete: {len(all_edge_records)} total edges in {elapsed:.1f}s ({elapsed / 60:.1f}m)")

    # Measure graph metrics on target graph
    wiki_only_gaining_edges = 0
    total_wiki_edges = 0
    if not args.dry_run:
        q_edges = "MATCH ()-[r:RELATES_TO {source: 'wiki'}]->() RETURN count(r) AS c"
        res_edges = await graphiti.driver.execute_query(q_edges)
        total_wiki_edges = res_edges[0][0]["c"] if res_edges and res_edges[0] else 0

        # Wiki-only entities (nodes with no episode MENTIONS) that now have >= 1 RELATES_TO
        q_wiki_only = """
        MATCH (e:Entity)
        WHERE NOT (e)<-[:MENTIONS]-(:Episodic)
          AND ((e)-[:RELATES_TO]-())
        RETURN count(DISTINCT e) AS c
        """
        res_wo = await graphiti.driver.execute_query(q_wiki_only)
        wiki_only_gaining_edges = res_wo[0][0]["c"] if res_wo and res_wo[0] else 0

    # Sample 50 edges for the user's review
    sample_50 = random.sample(all_edge_records, min(50, len(all_edge_records)))
    with open(args.sample_out, "w", encoding="utf-8") as f:
        json.dump(sample_50, f, indent=2)
    logger.info(f"Saved 50 sampled edges to {args.sample_out}")

    print("\n" + "=" * 60)
    print("PHASE 4 PILOT RESULTS SUMMARY")
    print("=" * 60)
    print(f"Target Graph:               {args.graph}")
    print(f"Notes Processed:            {len(target_notes)}")
    print(f"Sections Processed:         {len(all_tasks)}")
    print(f"Total Edges Extracted:      {len(all_edge_records)}")
    print(f"Total Wiki Edges in Graph:  {total_wiki_edges}")
    print(f"Wiki-only Entities Gaining Edges: {wiki_only_gaining_edges}")
    print(f"Elapsed Time:               {elapsed:.1f}s ({elapsed / 60:.1f}m)")
    print("\nEdges per Note:")
    for np, cnt in sorted(edges_per_note.items(), key=lambda x: -x[1]):
        print(f"  {cnt:3d} edges: {np}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
