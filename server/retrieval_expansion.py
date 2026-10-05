from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

import falkordb

from server.core.config import load_config
from server.providers.falkor_driver import falkordb_connection_params
from server.providers.memory_graphiti import (
    _provenance_from_source_description,
    resolve_target_database,
)
from server.providers.wiki.corpus import ExtractionStatus, SearchResult, get_corpus_root

logger = logging.getLogger(__name__)


def _extract_lede(text: str, max_chars: int = 240) -> str:
    """Extract a concise first sentence or lede from text, skipping frontmatter."""
    clean_text = text.strip()
    if clean_text.startswith("---"):
        end_fm = clean_text.find("\n---", 3)
        if end_fm != -1:
            clean_text = clean_text[end_fm + 4:].strip()

    clean = " ".join(clean_text.split())
    if not clean:
        return ""
    # Try finding first sentence
    for end_char in (". ", "; ", "\n"):
        idx = clean.find(end_char)
        if 20 <= idx <= max_chars:
            return clean[: idx + 1]
    if len(clean) > max_chars:
        return clean[:max_chars].rstrip() + "..."
    return clean


def expand_episodes_to_notes(
    episode_names: list[str],
    exclude_paths: set[str],
    max_expanded: int = 3,
    target_graph: Optional[str] = None,
) -> list[SearchResult]:
    """1-hop retrieval expansion: Episode -> Note through shared entities with INF weighting.

    Args:
        episode_names: List of episode names retrieved by direct memory recall.
        exclude_paths: Relative paths of notes already retrieved by direct search.
        max_expanded: Maximum number of expanded notes to return (default 3).
        target_graph: Optional target graph name override.
    """
    if not episode_names or max_expanded <= 0:
        return []

    graph_name = target_graph or resolve_target_database()
    try:
        fdb = falkordb.FalkorDB(**falkordb_connection_params())
        g = fdb.select_graph(graph_name)
        q = """
        MATCH (ep:Episodic)-[:MENTIONS]->(e:Entity)
        WHERE ep.name IN $ep_names
        WITH DISTINCT e
        MATCH (e)<-[:MENTIONS]-(n:Note)
        WITH e, collect(DISTINCT n.note_path) AS notes, count(DISTINCT n) AS note_freq
        RETURN e.name, note_freq, notes
        """
        res = g.ro_query(q, {"ep_names": episode_names}).result_set
    except Exception as exc:
        logger.debug("expand_episodes_to_notes failed (%s); continuing without expansion", exc)
        return []

    # Accumulate continuous INF score per note path: sum(1.0 / note_freq)
    note_candidates: dict[str, dict[str, Any]] = {}
    for row in res:
        ename = str(row[0])
        nfreq = int(row[1])
        notes = row[2] or []
        inf_weight = 1.0 / max(nfreq, 1)

        for np in notes:
            np_str = str(np)
            if np_str in exclude_paths:
                continue
            if np_str not in note_candidates:
                note_candidates[np_str] = {
                    "path": np_str,
                    "score": 0.0,
                    "top_entity": ename,
                    "top_entity_freq": nfreq,
                    "top_entity_weight": inf_weight,
                }
            note_candidates[np_str]["score"] += inf_weight
            if inf_weight > note_candidates[np_str]["top_entity_weight"]:
                note_candidates[np_str]["top_entity"] = ename
                note_candidates[np_str]["top_entity_freq"] = nfreq
                note_candidates[np_str]["top_entity_weight"] = inf_weight

    sorted_candidates = sorted(
        note_candidates.values(), key=lambda c: c["score"], reverse=True
    )[:max_expanded]

    try:
        corpus_root = get_corpus_root()
    except Exception:
        corpus_root = None

    results: list[SearchResult] = []
    for cand in sorted_candidates:
        rel_path = cand["path"]
        ename = cand["top_entity"]
        score = cand["score"]

        # Attempt to read snippet from file on disk
        snippet = None
        if corpus_root:
            full_path = corpus_root / rel_path
            if full_path.is_file():
                try:
                    text = full_path.read_text(encoding="utf-8", errors="replace")
                    snippet = _extract_lede(text, max_chars=300)
                except Exception:
                    pass

        path_obj = Path(rel_path)
        top_level = path_obj.parts[0] if len(path_obj.parts) > 1 else "ROOT"
        results.append(
            SearchResult(
                source="durable_knowledge",
                relative_path=rel_path,
                filename=path_obj.name,
                top_level_area=top_level,
                media_type="text/markdown",
                extractor="file",
                extraction_status=ExtractionStatus.EXTRACTED.value if snippet else ExtractionStatus.UNSUPPORTED.value,
                matched_snippet=snippet or f"[Connected via entity '{ename}']",
                match_basis=f"expanded via entity: {ename} (INF: {score:.2f})",
                query="",
                relevance_score=round(score, 2),
            )
        )

    return results


def expand_notes_to_episodes(
    note_paths: list[str],
    exclude_episode_names: set[str],
    max_expanded: int = 3,
    target_graph: Optional[str] = None,
) -> list[dict[str, Any]]:
    """1-hop retrieval expansion: Note -> Episode through shared entities with INF weighting.

    Args:
        note_paths: List of relative note paths retrieved by direct wiki search.
        exclude_episode_names: Names of episodes already retrieved by direct memory recall.
        max_expanded: Maximum number of expanded memory facts to return (default 3).
        target_graph: Optional target graph name override.
    """
    if not note_paths or max_expanded <= 0:
        return []

    graph_name = target_graph or resolve_target_database()
    try:
        fdb = falkordb.FalkorDB(**falkordb_connection_params())
        g = fdb.select_graph(graph_name)
        q = """
        MATCH (n:Note)-[:MENTIONS]->(e:Entity)
        WHERE n.note_path IN $wiki_paths
        WITH DISTINCT e
        MATCH (e)<-[:MENTIONS]-(all_n:Note)
        WITH e, count(DISTINCT all_n) AS note_freq
        MATCH (e)<-[:MENTIONS]-(ep:Episodic)
        RETURN e.name, note_freq, ep.name, ep.content, ep.valid_at, ep.source_description
        """
        res = g.ro_query(q, {"wiki_paths": note_paths}).result_set
    except Exception as exc:
        logger.debug("expand_notes_to_episodes failed (%s); continuing without expansion", exc)
        return []

    ep_candidates: dict[str, dict[str, Any]] = {}
    for row in res:
        ename = str(row[0])
        nfreq = int(row[1])
        ep_name = str(row[2]) if row[2] else ""
        ep_content = str(row[3]) if row[3] else ""
        ep_valid_at = str(row[4]) if row[4] else "N/A"
        ep_sd = str(row[5]) if row[5] else ""
        inf_weight = 1.0 / max(nfreq, 1)

        if not ep_name or ep_name in exclude_episode_names:
            continue

        if ep_name not in ep_candidates:
            ep_candidates[ep_name] = {
                "name": ep_name,
                "content": ep_content,
                "valid_at": ep_valid_at,
                "source_description": ep_sd,
                "score": 0.0,
                "top_entity": ename,
                "top_entity_weight": inf_weight,
            }
        ep_candidates[ep_name]["score"] += inf_weight
        if inf_weight > ep_candidates[ep_name]["top_entity_weight"]:
            ep_candidates[ep_name]["top_entity"] = ename
            ep_candidates[ep_name]["top_entity_weight"] = inf_weight

    sorted_candidates = sorted(
        ep_candidates.values(), key=lambda c: c["score"], reverse=True
    )[:max_expanded]

    facts: list[dict[str, Any]] = []
    for cand in sorted_candidates:
        ep_name = cand["name"]
        ep_content = cand["content"]
        ep_valid_at = cand["valid_at"]
        ep_sd = cand["source_description"]
        ename = cand["top_entity"]
        score = cand["score"]

        prov_str = _provenance_from_source_description(ep_sd)
        exp_prov = f"expanded via entity: {ename} (INF: {score:.2f})"
        full_prov = f"{exp_prov} · {prov_str}" if prov_str else exp_prov

        lede = _extract_lede(ep_content, max_chars=200)
        facts.append({
            "fact": f"[Expanded via entity: {ename}] {lede}",
            "valid_at": ep_valid_at,
            "invalid_at": None,
            "source_episodes": [{
                "name": ep_name,
                "content": ep_content,
                "provenance": full_prov,
            }],
            "episode_names": [ep_name],
            "is_expanded": True,
            "via_entity": ename,
            "score": score,
        })

    return facts
