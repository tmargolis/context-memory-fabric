"""MS7b Phase 4 -- duplicate merge + retention sweep over episode-extracted entities,
plus Note-date upkeep.

Two entity passes, in this order (merge before retention matters: a merged
entity's *combined* mention count is what the retention sweep should
judge, not two fragments each independently looking weaker than the
entity actually is):

1. **Merge** every duplicate-name :Entity group onto one canonical node
   (see find_duplicate_groups/merge_duplicate_entities docstrings for the
   group_id root cause found 2026-09-13, and why this repairs the graph
   rather than requiring a full replay redo).
2. **Retention sweep** over what's left. Wiki-seeded entities
   (scripts/seed_wiki_graph.py, `source='wiki'`) are never judged here --
   the wiki registry is their vouching. This only judges entities
   Graphiti's own extraction minted fresh during episode replay
   (scripts/rebuild_graph_from_ledger.py into the seeded graph) --
   i.e. every :Entity node WITHOUT `source='wiki'`.

Retention rule (the user, 2026-09-13):
  - recurring across >= --min-episodes distinct episodes (default 2) -> keep
  - OR vouched by wiki prose, via IDF-weighted document-frequency matching
    (NOT naive substring -- that inflated an earlier exploratory count in
    this same working session, where "table" and "wood" self-vouched
    simply for being English words that appear everywhere)
  - otherwise -> HOLD as a candidate, never deleted. Marked
    `ms7b_status='candidate'` (wiki-vouched/recurring entities get
    `ms7b_status='confirmed'`) so it stays fully inspectable and can be
    promoted later on further corroboration, per the user's explicit call on
    the 415 non-wiki singletons this was measured against.

IDF vouching: for each candidate entity name, count how many notes across
the WHOLE LLM_Wiki vault contain it as a whole-word/phrase match, then
score idf = ln((N+1)/(df+1)). A generic word occurring in a large fraction
of notes scores low; a specific, rare name scores high. This is a
one-time scan of the vault's text (no LLM, no network) cached in memory
for the run.

3. **Note dates** (B03, 2026-10-10): every :Note's created_at / updated_at is
   re-resolved from the wiki (server/providers/wiki/note_dates.py) and
   rewritten where it changed. A known date is never replaced by "unknown"
   (e.g. a note whose file was deleted).
   `--note-dates-only` runs just this pass, which is how an existing graph's
   notes get their first dates. `--repath-notes` first moves :Note nodes whose
   file was moved to its new path (repath_notes).

Usage:
    uv run python scripts/sweep_wiki_graph.py --graph-name mem-fabric-local-wiki
        [--min-episodes 2] [--idf-threshold 2.0] [--dry-run] [--note-dates-only]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

DEFAULT_MIN_EPISODES = 2
DEFAULT_IDF_THRESHOLD = 2.0  # roughly: vouched if it appears in <~13% of notes


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _build_vault_index(root: Path) -> tuple[int, dict[str, int]]:
    """(note_count, {normalized_word: document_frequency}) across every .md
    file in the vault -- not just the WIKI/REPORTS/TO-RESEARCH scan scope,
    matching the broader vouching intent (a mention in RAW/ still counts).
    Word-boundary matched, so "art" inside "start" doesn't count."""
    from server.providers.wiki.corpus import IGNORED_DIR_NAMES

    df: dict[str, int] = {}
    note_count = 0
    for p in root.rglob("*.md"):
        if any(part in IGNORED_DIR_NAMES or part.startswith(".") for part in p.parts):
            continue
        try:
            text = _norm(p.read_text(errors="ignore"))
        except Exception:
            continue
        note_count += 1
        # distinct normalized words present in this note (set, so repeats
        # within one note don't inflate document frequency)
        for w in set(text.split()):
            df[w] = df.get(w, 0) + 1
    return note_count, df


def _phrase_df(phrase_norm: str, note_count: int, word_df: dict[str, int]) -> int:
    """Approximate document frequency for a (possibly multi-word) phrase as
    the MINIMUM document frequency among its words -- a conservative
    (upper-bound-on-idf-penalty) stand-in for true phrase-level df, cheap
    to compute from a word-level index. A multi-word phrase is at least as
    rare as its rarest word, so this never over-vouches a phrase whose
    individual words are all common but never co-occur."""
    words = phrase_norm.split()
    if not words:
        return note_count
    return min(word_df.get(w, 0) for w in words)


def idf_score(phrase: str, note_count: int, word_df: dict[str, int]) -> float:
    df = _phrase_df(_norm(phrase), note_count, word_df)
    return math.log((note_count + 1) / (df + 1))


def _group_and_pick_canonical(recs: list[dict]) -> list[tuple[dict, list[dict]]]:
    """Pure grouping/canonical-selection logic, split out from
    find_duplicate_groups so it's testable without a live driver."""
    groups: dict[str, list[dict]] = {}
    for r in recs:
        groups.setdefault(_norm(r["name"]), []).append(r)

    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        wiki_members = [m for m in members if m["source"] == "wiki"]
        # Prefer a wiki-seeded canonical (it carries the durable-knowledge
        # grounding this whole layer is for); otherwise the earliest-created
        # node, so an entity's history reads as continuous rather than
        # picking an arbitrary survivor.
        canonical = wiki_members[0] if wiki_members else sorted(members, key=lambda m: m["created_at"] or "")[0]
        duplicates = [m for m in members if m["uuid"] != canonical["uuid"]]
        out.append((canonical, duplicates))
    return out


async def find_duplicate_groups(driver) -> list[tuple[dict, list[dict]]]:
    """Group :Entity nodes by normalized name; return (canonical, duplicates)
    for every group with more than one member.

    Root cause (found 2026-09-13): scripts/seed_wiki_graph.py originally
    seeded entities with group_id="" while graphiti_core's own add_episode()
    defaults extracted entities to group_id="_" (nothing in this codebase
    ever passes an explicit group_id to remember()). Graphiti's own
    entity-resolution/dedup search is scoped BY group_id, so a wiki-seeded
    node was never even considered a merge candidate during replay --
    regardless of name match quality. Confirmed directly: two "Anthropic"
    nodes, byte-identical name, group_id "" vs "_". Fixed going forward in
    seed_wiki_graph.py; this repairs the graph that already exists from the
    group_id="" seed. Uses the same _norm() as the IDF vouching below, so
    it also catches the separate Unicode-whitespace case (a stray
    non-breaking space, e.g. "NVIDIA\xa0Spark" vs "NVIDIA Spark").
    """
    rows = await driver.execute_query(
        "MATCH (e:Entity) RETURN e.uuid AS uuid, e.name AS name, e.source AS source, e.created_at AS created_at"
    )
    recs = rows[0] if rows and isinstance(rows[0], list) else (rows or [])
    return _group_and_pick_canonical(recs)


async def merge_duplicate_entities(driver, groups: list[tuple[dict, list[dict]]]) -> dict:
    """Redirect every edge off each duplicate onto its group's canonical
    node, preserving RELATES_TO's fact/episodes/embedding properties via
    `SET r2 = properties(r)`, then delete the now-edgeless duplicate.
    MENTIONS and RELATES_TO are the only edge types touching :Entity in
    this graph (verified directly against the live schema before writing
    this) -- nothing else needs redirecting.
    """
    edges_redirected = 0
    for canonical, duplicates in groups:
        for dup in duplicates:
            r1 = await driver.execute_query(
                "MATCH (x)-[r:MENTIONS]->(d:Entity {uuid:$d}) "
                "MATCH (c:Entity {uuid:$c}) "
                "MERGE (x)-[:MENTIONS]->(c) "
                "DELETE r "
                "RETURN count(r) AS n",
                d=dup["uuid"], c=canonical["uuid"],
            )
            r2 = await driver.execute_query(
                "MATCH (d:Entity {uuid:$d})-[r:RELATES_TO]->(y) "
                "MATCH (c:Entity {uuid:$c}) "
                "CREATE (c)-[r2:RELATES_TO]->(y) SET r2 = properties(r) "
                "DELETE r "
                "RETURN count(r) AS n",
                d=dup["uuid"], c=canonical["uuid"],
            )
            r3 = await driver.execute_query(
                "MATCH (y)-[r:RELATES_TO]->(d:Entity {uuid:$d}) "
                "MATCH (c:Entity {uuid:$c}) "
                "CREATE (y)-[r2:RELATES_TO]->(c) SET r2 = properties(r) "
                "DELETE r "
                "RETURN count(r) AS n",
                d=dup["uuid"], c=canonical["uuid"],
            )
            for res in (r1, r2, r3):
                recs = res[0] if res and isinstance(res[0], list) else (res or [])
                if recs:
                    edges_redirected += recs[0]["n"]
            await driver.execute_query("MATCH (d:Entity {uuid:$d}) DELETE d", d=dup["uuid"])

    return {
        "duplicate_groups_merged": len(groups),
        "entities_removed": sum(len(d) for _, d in groups),
        "edges_redirected": edges_redirected,
    }


async def refresh_note_dates(driver, wiki_root: Path, dry_run: bool) -> dict:
    """Re-resolve every :Note's dates and write the ones that changed."""
    from collections import Counter
    from server.providers.wiki.note_dates import NOTE_DATE_FIELDS, git_index, resolve_note_dates, write_note_dates

    res = await driver.execute_query(
        "MATCH (n:Note) RETURN n.note_path AS note_path, "
        + ", ".join(f"n.{k} AS {k}" for k in NOTE_DATE_FIELDS)
    )
    current = res[0] if res and isinstance(res[0], list) else (res or [])
    index = git_index(wiki_root)
    changed, undated, sources = [], 0, Counter()
    for rec in current:
        path = rec["note_path"]
        if not path:
            continue
        resolved = resolve_note_dates(wiki_root, path, index)
        if resolved["created_at"] is None:
            undated += 1  # no file and no history: leave whatever the node has
            continue
        sources[resolved["created_source"]] += 1
        want = {k: resolved[k] for k in NOTE_DATE_FIELDS}
        if any(rec.get(k) != want[k] for k in NOTE_DATE_FIELDS):
            changed.append({"note_path": path, **want})
    result = {"notes": len(current), "dated": sum(sources.values()), "undated": undated,
              "created_sources": dict(sources), "changed": len(changed)}
    if dry_run:
        logger.info(f"[dry-run] note dates: {result}")
        for row in changed[:10]:
            logger.info(f"    {row['note_path']}: created {row['created_at']}, updated {row['updated_at']}")
        return result
    result["written"] = await write_note_dates(driver, changed)
    logger.info(f"Note dates: {result}")
    return result


def _note_id(note_path: str) -> str:
    """Same id scripts/build_wiki_sections.py gives a note path."""
    import hashlib
    return hashlib.sha1(note_path.encode("utf-8")).hexdigest()[:16]


async def repath_notes(driver, wiki_root: Path, dry_run: bool, log_dir: Optional[Path] = None) -> dict:
    """Point :Note nodes whose file moved at its new path (found 2026-10-10: a
    folder move left 27 stubs naming files that now live elsewhere).

    A note whose file is missing is moved when exactly one file with the same
    name exists elsewhere in the vault and no :Note already has that path.
    note_path, name and note_id (sha1 of the path, as build_wiki_sections.py
    assigns it) change together, as does note_path on the note's :Section
    nodes; edges are untouched. Ambiguous, missing or colliding paths are
    reported and left alone. An applied run writes an old -> new mapping to
    `log_dir` for undo."""
    from collections import defaultdict
    from datetime import datetime, timezone

    res = await driver.execute_query("MATCH (n:Note) RETURN n.note_path AS note_path")
    current = res[0] if res and isinstance(res[0], list) else (res or [])
    paths = {r["note_path"] for r in current if r["note_path"]}
    by_name: dict[str, list[str]] = defaultdict(list)
    for f in wiki_root.rglob("*"):
        rel = f.relative_to(wiki_root)
        if f.is_file() and not any(part.startswith(".") for part in rel.parts):
            by_name[f.name].append(rel.as_posix())
    moves, skipped = [], []
    for old in sorted(paths):
        if (wiki_root / old).exists():
            continue
        found = by_name.get(Path(old).name, [])
        if len(found) == 1 and found[0] not in paths:
            moves.append({"old": old, "new": found[0], "old_id": _note_id(old), "new_id": _note_id(found[0])})
        else:
            reason = "no file with that name" if not found else ("ambiguous" if len(found) > 1 else "target already a Note")
            skipped.append({"path": old, "reason": reason, "candidates": found[:5]})
    result = {"missing": len(moves) + len(skipped), "moves": len(moves), "skipped": skipped}
    if dry_run:
        for m in moves[:10]:
            logger.info(f"[dry-run] {m['old']} -> {m['new']}")
        return result
    for m in moves:
        await driver.execute_query(
            "MATCH (n:Note {note_path: $old}) SET n.note_path = $new, n.name = $new, n.note_id = $new_id",
            old=m["old"], new=m["new"], new_id=m["new_id"],
        )
        await driver.execute_query("MATCH (s:Section {note_path: $old}) SET s.note_path = $new", old=m["old"], new=m["new"])
    if moves and log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / f"note_repath_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
        log.write_text(json.dumps(moves, indent=1) + "\n")
        result["log"] = str(log)
    logger.info(f"Repathed {len(moves)} notes; skipped {len(skipped)}")
    return result


async def sweep(graph_name: str, min_episodes: int, idf_threshold: float, wiki_root: Path, dry_run: bool,
                note_dates_only: bool = False, repath: bool = False) -> dict:
    from server.providers.memory_graphiti import get_graphiti

    graphiti = get_graphiti(graph_name=graph_name)
    driver = graphiti.driver
    out: dict = {"dry_run": dry_run}
    if repath:  # before dates, so a moved note is dated from its new path
        out["repath"] = await repath_notes(driver, wiki_root, dry_run, log_dir=Path("imports/state"))
    if note_dates_only:
        out["note_dates"] = await refresh_note_dates(driver, wiki_root, dry_run)
        return out

    dup_groups = await find_duplicate_groups(driver)
    logger.info(f"Duplicate-name entity groups: {len(dup_groups)} "
                f"({sum(len(d) for _, d in dup_groups)} redundant entities)")
    if dry_run:
        logger.info("[dry-run] sample duplicate groups (canonical <- duplicates):")
        for canonical, duplicates in dup_groups[:15]:
            logger.info(f"    {canonical['name']!r} <- {[d['name'] for d in duplicates]}")
    else:
        merge_result = await merge_duplicate_entities(driver, dup_groups)
        logger.info(f"Merged: {merge_result}")

    rows = await driver.execute_query(
        "MATCH (e:Entity) WHERE e.source IS NULL OR e.source <> 'wiki' "
        "OPTIONAL MATCH (e)<-[:MENTIONS]-(ep:Episodic) "
        "RETURN e.uuid AS uuid, e.name AS name, count(DISTINCT ep) AS episode_mentions"
    )
    recs = rows[0] if rows and isinstance(rows[0], list) else (rows or [])

    logger.info(f"Building vault-wide document-frequency index from {wiki_root} ...")
    note_count, word_df = _build_vault_index(wiki_root)
    logger.info(f"  {note_count} notes indexed")

    confirmed_recurring, confirmed_vouched, candidates = [], [], []
    for r in recs:
        name, mentions = r["name"], r["episode_mentions"]
        if mentions >= min_episodes:
            confirmed_recurring.append(r)
            continue
        idf = idf_score(name, note_count, word_df)
        if idf <= idf_threshold:  # low idf = common word = NOT vouched
            candidates.append(r)
        else:
            confirmed_vouched.append(r)

    logger.info(f"Non-wiki entities: {len(recs)}")
    logger.info(f"  confirmed (>= {min_episodes} episodes): {len(confirmed_recurring)}")
    logger.info(f"  confirmed (IDF-vouched by wiki prose):  {len(confirmed_vouched)}")
    logger.info(f"  candidate (held, not deleted):          {len(candidates)}")

    if dry_run:
        logger.info("[dry-run] sample candidates:")
        for r in candidates[:20]:
            logger.info(f"    {r['name']}")
        return {
            "dry_run": True,
            "duplicate_groups": len(dup_groups), "duplicate_entities": sum(len(d) for _, d in dup_groups),
            "non_wiki_entities": len(recs),
            "confirmed_recurring": len(confirmed_recurring),
            "confirmed_vouched": len(confirmed_vouched),
            "candidates": len(candidates),
            "note_dates": await refresh_note_dates(driver, wiki_root, dry_run=True),
            **({"repath": out["repath"]} if repath else {}),
        }

    async def _tag(uuids: list[str], status: str):
        if not uuids:
            return
        await driver.execute_query(
            "UNWIND $uuids AS u MATCH (e:Entity {uuid: u}) SET e.ms7b_status = $status",
            uuids=uuids, status=status,
        )

    await _tag([r["uuid"] for r in confirmed_recurring] + [r["uuid"] for r in confirmed_vouched], "confirmed")
    await _tag([r["uuid"] for r in candidates], "candidate")
    # wiki-seeded entities are confirmed by construction
    await driver.execute_query(
        "MATCH (e:Entity {source: 'wiki'}) WHERE e.ms7b_status IS NULL SET e.ms7b_status = 'confirmed'"
    )

    return {
        "dry_run": False,
        "merge": merge_result,
        "non_wiki_entities": len(recs),
        "confirmed_recurring": len(confirmed_recurring),
        "confirmed_vouched": len(confirmed_vouched),
        "candidates": len(candidates),
        "note_dates": await refresh_note_dates(driver, wiki_root, dry_run=False),
        **({"repath": out["repath"]} if repath else {}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--graph-name", required=True)
    parser.add_argument("--min-episodes", type=int, default=DEFAULT_MIN_EPISODES)
    parser.add_argument("--idf-threshold", type=float, default=DEFAULT_IDF_THRESHOLD)
    parser.add_argument("--wiki-root", type=Path, default=None, help="defaults to LLM_WIKI_PATH")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--note-dates-only", action="store_true", help="Only refresh :Note dates (pass 3)")
    parser.add_argument("--repath-notes", action="store_true",
                        help="First point :Note nodes whose file moved at its new path (unique file name match)")
    args = parser.parse_args()

    os.environ["FALKORDB_DATABASE"] = args.graph_name

    from server.providers.wiki.corpus import get_corpus_root
    wiki_root = args.wiki_root or get_corpus_root()

    result = asyncio.run(sweep(args.graph_name, args.min_episodes, args.idf_threshold, wiki_root, args.dry_run,
                               note_dates_only=args.note_dates_only, repath=args.repath_notes))
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
