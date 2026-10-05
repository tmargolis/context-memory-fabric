"""MS9 Phase 3 -- Cheap Linking Evaluation over Group C (spanning) queries.

Tests 1-hop cross-tier retrieval expansion through shared entities with INF weighting in get_context:
- Durable Knowledge: direct up to 5, expanded up to 3 (total cap: 8)
- Episodic Memory: direct up to 10, expanded up to 3 (total cap: 13)
- Measures baseline vs expanded Hit rate on Group C.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

import falkordb

from server.context import get_context
from server.providers.memory_graphiti import resolve_target_database
from server.providers.falkor_driver import falkordb_connection_params


def _extract_wiki_paths_from_context(ctx_text: str) -> list[str]:
    """Parse relative note paths from Durable Knowledge section."""
    paths = []
    # Lines like: ### 1. `WIKI/projects/Atlas.md` (WIKI)
    pat = re.compile(r"^###\s+\d+\.\s+`([^`]+)`", re.MULTILINE)
    for match in pat.finditer(ctx_text):
        paths.append(match.group(1))
    return paths


def _extract_episode_names_from_context(ctx_text: str) -> list[str]:
    """Parse episode names mentioned in Episodic Memory section."""
    ep_names = []
    # In source episodes or fact text: e.g. chatgpt-condo-001, claude-code-jspace-011, etc.
    pat = re.compile(r"\b((?:chatgpt|claude|gemini|claude-code|mcp)-[a-z0-9_-]+-\d{3,})\b")
    for match in pat.finditer(ctx_text):
        ep_names.append(match.group(1))
    return list(dict.fromkeys(ep_names))


async def evaluate_cheap_linking(
    cases_path: Path,
    graph_name: str,
    limit: int | None = None,
) -> dict:
    fdb = falkordb.FalkorDB(**falkordb_connection_params())
    g = fdb.select_graph(graph_name)
    
    res_n = g.ro_query("MATCH (n:Note) RETURN count(n)").result_set
    total_notes = res_n[0][0]
    print(f"Graph: {graph_name} ({total_notes} notes indexed)")
    
    with cases_path.open() as f:
        data = json.load(f)
    cases = [q for q in data["queries"] if q.get("group") == "C-spanning"]
    if limit:
        cases = cases[:limit]
        
    print(f"\nEvaluating {len(cases)} Group C (spanning) queries via get_context():")
    print("Durable Knowledge: direct up to 5, expanded up to 3 (total cap 8)")
    print("Episodic Memory:   direct up to 10, expanded up to 3 (total cap 13)")
    print("=" * 80)
    
    results = []
    
    for case in cases:
        cid = case["id"]
        query = case["query"]
        gold_episodes = case.get("gold_episodes") or []
        gold_wiki = case.get("gold_wiki") or []
        
        # 1. Baseline: Direct only (no expansion)
        ctx_base = await get_context(
            topic=query,
            max_wiki_results=5,
            max_memory_results=10,
            max_expanded_wiki=0,
            max_expanded_memory=0,
        )
        base_wiki_paths = _extract_wiki_paths_from_context(ctx_base)
        base_ep_names = _extract_episode_names_from_context(ctx_base)
        base_w_hit = any(gw in base_wiki_paths for gw in gold_wiki)
        base_m_hit = any(ge in base_ep_names for ge in gold_episodes)
        
        # 2. MS9 Phase 3: With 1-hop cross-tier expansion (5+3 wiki, 10+3 memory)
        ctx_exp = await get_context(
            topic=query,
            max_wiki_results=5,
            max_memory_results=10,
            max_expanded_wiki=3,
            max_expanded_memory=3,
        )
        exp_wiki_paths = _extract_wiki_paths_from_context(ctx_exp)
        exp_ep_names = _extract_episode_names_from_context(ctx_exp)
        exp_w_hit = any(gw in exp_wiki_paths for gw in gold_wiki)
        exp_m_hit = any(ge in exp_ep_names for ge in gold_episodes)
        
        tag = ""
        if not base_w_hit and exp_w_hit:
            tag += " [WIKI RECOVERED]"
        if not base_m_hit and exp_m_hit:
            tag += " [MEM RECOVERED]"
            
        print(f"{cid:<4} base: (W={int(base_w_hit)}, M={int(base_m_hit)}) -> expanded: (W={int(exp_w_hit)}, M={int(exp_m_hit)}) [W_cnt: {len(exp_wiki_paths)}, M_cnt: {len(exp_ep_names)}]{tag}")
        
        results.append({
            "id": cid,
            "base_w_hit": base_w_hit,
            "base_m_hit": base_m_hit,
            "exp_w_hit": exp_w_hit,
            "exp_m_hit": exp_m_hit,
            "exp_wiki_paths": exp_wiki_paths,
            "exp_ep_names": exp_ep_names,
        })
        
    print("=" * 80)
    w_before = sum(1 for r in results if r["base_w_hit"])
    w_after = sum(1 for r in results if r["exp_w_hit"])
    m_before = sum(1 for r in results if r["base_m_hit"])
    m_after = sum(1 for r in results if r["exp_m_hit"])
    total = len(results)
    print(f"Wiki Hits: {w_before}/{total} ({w_before/total*100:.1f}%) -> {w_after}/{total} ({w_after/total*100:.1f}%) [+{w_after - w_before}]")
    print(f"Mem Hits:  {m_before}/{total} ({m_before/total*100:.1f}%) -> {m_after}/{total} ({m_after/total*100:.1f}%) [+{m_after - m_before}]")
    
    return {"results": results, "wiki_before": w_before, "wiki_after": w_after, "mem_before": m_before, "mem_after": m_after}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=str(_ROOT / "tests" / "fixtures" / "ms7_eval" / "queries.json"))
    parser.add_argument("--graph", default=resolve_target_database())
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    
    asyncio.run(evaluate_cheap_linking(
        cases_path=Path(args.cases),
        graph_name=args.graph,
        limit=args.limit,
    ))


if __name__ == "__main__":
    main()
