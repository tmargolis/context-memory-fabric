"""Read-only B16 project inference comparison; local outputs contain no queries.

Uses B08's recall_mem(max_results=24) pool and boost. The provider currently
returns at most ten edge and six vector candidates: 24 is a requested width,
not a guaranteed pool size. Record actual sizes rather than changing recall.
E reuses the vector arm's embedding through a process-local caching wrapper.
No extra inference or embedding calls are introduced by the five methods.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import math
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.experiment_b16_readonly import readonly_graphiti
from scripts.experiment_project_rerank_replay import boost, episode_projects, fact_project, oracle_project
from server.replay.grading import grade_case, summarize
from server.replay.runner import _WikiIndex, resolve_gold
from server.replay.snapshot import graph_episode_rows, memory_names


def normalized(text):
    words = re.findall(r"[a-z0-9]+", text.lower())
    # Conservative English plural normalization (avoid shortening e.g. 'analysis').
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "is", "us")) else w
                    for w in words)


def contains(query, name):
    term = normalized(name)
    return bool(term) and f" {term} " in f" {normalized(query)} "


def choose(scores, threshold):
    ranked = sorted(((p, s) for p, s in scores.items() if s > 0), key=lambda x: (-x[1], x[0]))
    if not ranked:
        return None, 0.0
    total = sum(s for _, s in ranked)
    top = ranked[0][1]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = (top - second) / total
    return (ranked[0][0] if margin > threshold else None), margin


def alias_scores(query, projects, aliases):
    result = Counter()
    for project in projects:
        labels = {project} | {a for a, p in aliases.items() if p == project}
        if any(contains(query, label) for label in labels):
            result[project] = 1.0
    return result


def entity_scores(query, entities, sizes):
    result = Counter()
    for name, projects in entities:
        if contains(query, name):
            for p in set(projects):
                result[p] += 1.0 / max(sizes[p], 1)
    return result


def candidate_scores(pool, proj_of):
    result = Counter()
    for rank, fact in enumerate(pool, 1):
        if p := fact_project(fact, proj_of):
            result[p] += 1.0 / rank
    return result


def centroid_scores(vector, centroids):
    if vector is None:
        return {}
    norm = math.sqrt(sum(x*x for x in vector))
    if norm == 0:
        return {}
    result = {}
    for p, center in centroids.items():
        if len(center) != len(vector):
            raise ValueError("Query and centroid embedding dimensions differ")
        cnorm = math.sqrt(sum(x*x for x in center))
        if cnorm:
            result[p] = max(0.0, sum(a*b for a, b in zip(vector, center)) / norm / cnorm)
    return result


def build_centroids(rows, width=768):
    sums, counts = {}, Counter()
    for project, vector in rows:
        if not project or vector is None:
            continue
        if len(vector) != width:
            raise ValueError("Stored episode embedding dimension differs from 768")
        if project not in sums:
            sums[project] = [0.0] * width
        sums[project] = [a+b for a, b in zip(sums[project], vector)]
        counts[project] += 1
    return {p: [v/counts[p] for v in values] for p, values in sums.items()}


def inference_metrics(predictions):
    """Do not call an unlabeled project's guess a known classification error."""
    labeled = [p for p in predictions if p['gold_project'] is not None]
    guessed = [p for p in labeled if p['guess'] is not None]
    wrong = [p for p in guessed if p['guess'] != p['gold_project']]
    unlabeled_guesses = [p['case_id'] for p in predictions if p['gold_project'] is None and p['guess'] is not None]
    return {
        'project_labeled_cases':len(labeled),
        'project_labeled_guesses':len(guessed),
        'accuracy_when_guessed':1-len(wrong)/len(guessed) if guessed else None,
        'abstain_rate':sum(p['guess'] is None for p in predictions)/len(predictions),
        'confident_wrong_rate':len(wrong)/len(labeled) if labeled else None,
        'wrong_case_ids':[p['case_id'] for p in wrong],
        'guesses_without_gold_project':unlabeled_guesses,
        'conservative_wrong_or_unlabeled_rate':(len(wrong)+len(unlabeled_guesses))/len(predictions),
    }


class CachedEmbedder:
    """Cache calls already made by recall; never independently embed a query."""
    def __init__(self, wrapped, cache):
        self.wrapped, self.cache = wrapped, cache

    async def create(self, input_data):
        key = input_data if isinstance(input_data, str) else json.dumps(input_data)
        if key not in self.cache:
            self.cache[key] = await self.wrapped.create(input_data)
        return self.cache[key]

    def __getattr__(self, name):
        return getattr(self.wrapped, name)


async def run(args):
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    os.environ["FALKORDB_DATABASE"] = args.graph
    from server.core.falkordb_conn import falkordb_client
    from server.core.config import load_config
    from server.consolidation.project_aliases import project_aliases
    from server.providers import memory_graphiti as mg
    from server.adapters.spark_lock import spark_slot
    from server.journal.store import DEFAULT_JOURNAL_PATH
    from scripts.experiment_b16_clusters import counts

    config = load_config()
    if config.embedding_dim != 768:
        raise RuntimeError("B16 requires the existing 768-dimensional configuration")
    if not config.llm_is_local or not config.embed_is_local:
        raise RuntimeError("B16 local run refuses external providers without a new ruling")
    out = Path(args.out)
    if not out.resolve().is_relative_to((ROOT / "imports/replay").resolve()):
        raise ValueError("Outputs must stay in the ignored imports/replay directory")
    out.parent.mkdir(parents=True, exist_ok=True)
    before = counts(args.graph)
    g = falkordb_client().select_graph(args.graph)
    proj_of = episode_projects(args.graph)
    cases, problems = resolve_gold(json.loads((ROOT / 'tests/fixtures/ms7_eval/queries.json').read_text())["queries"],
                                   memory_names(graph_episode_rows(args.gold_source or args.graph)))
    if problems:
        raise RuntimeError("Unresolved gold: " + "; ".join(problems))
    # Offline name dictionary/project-size build; each query does one batched
    # read of the matched entities' current project lists (method B).
    entities = g.ro_query("MATCH (n:Entity) WHERE n.project IS NOT NULL RETURN n.name, n.project").result_set
    entities = [(name, ps if isinstance(ps, list) else [ps]) for name, ps in entities]
    name_terms = [(name, f' {normalized(name)} ') for name,_ in entities]
    sizes = Counter(p for _, ps in entities for p in set(ps))
    centroids = build_centroids(g.ro_query(
        "MATCH (e:Episodic) WHERE e.project IS NOT NULL AND e.content_embedding IS NOT NULL "
        "RETURN e.project, e.content_embedding").result_set)
    aliases = project_aliases()
    mapping = {}
    gold_projects = proj_of
    if args.rollups:
        from scripts.experiment_b16_lineage import member_mapping
        mapping = member_mapping(args.rollups)
        gold_projects = episode_projects(args.gold_source or args.graph)
    wiki = _WikiIndex(Path(os.environ["LLM_WIKI_PATH"]).expanduser() if os.getenv("LLM_WIKI_PATH") else None)
    thresholds = {m:args.thresholds for m in 'ABCDE'}
    # Centroid cosine gaps are small after normalizing across all projects.
    thresholds['E'] = sorted(set(args.thresholds + [.0005,.001,.002,.005]))
    variants = ["baseline", "oracle"] + [f"{m}@{t:g}" for m in "ABCDE" for t in thresholds[m]]
    grades = {v: [] for v in variants}
    predictions = {v: [] for v in variants}
    embeddings, pool_sizes = {}, []
    costs_ms = {m:[] for m in 'ABCE'}
    # Local retrieval already only embeds; holding the shared lock also protects
    # Spark from overlap with pollers while the experiment is collecting pools.
    with readonly_graphiti():
        try:
            client = mg.get_graphiti(graph_name=args.graph)
            wrapped = CachedEmbedder(client.embedder, embeddings)
            client.embedder = wrapped
            client.clients.embedder = wrapped
            for i, case in enumerate(cases, 1):
                while True:
                    with spark_slot(DEFAULT_JOURNAL_PATH) as busy:
                        if busy is None:
                            pool = await mg.recall_mem(case["query"], max_results=args.pool, format_for_mcp=False)
                            break
                    print("Waiting for Spark slot", flush=True)
                    await asyncio.sleep(5)
                pool_sizes.append(len(pool))
                target = oracle_project(case, gold_projects)
                from scripts.experiment_b16_lineage import remap_case
                grading_case = remap_case(case, mapping)
                paths = [w.relative_path for w in wiki.search(case["query"], args.k)]
                start = time.perf_counter()
                norm_query = f' {normalized(case["query"])} '
                matched_names = [name for name, term in name_terms if term.strip() and term in norm_query]
                matched_entities = g.ro_query(
                    "MATCH (n:Entity) WHERE n.name IN $names AND n.project IS NOT NULL "
                    "RETURN n.name, n.project", params={"names": matched_names}).result_set
                matched_entities = [(name, ps if isinstance(ps, list) else [ps]) for name, ps in matched_entities]
                signals = {'B':entity_scores(case["query"],matched_entities,sizes)}
                costs_ms['B'].append((time.perf_counter()-start)*1000)
                for method, operation in [
                    ('A',lambda:alias_scores(case['query'],set(proj_of.values()),aliases)),
                    ('C',lambda:candidate_scores(pool,proj_of)),
                    ('E',lambda:centroid_scores(embeddings.get(case['query']),centroids)),
                ]:
                    start = time.perf_counter()
                    signals[method] = operation()
                    costs_ms[method].append((time.perf_counter()-start)*1000)
                for v in variants:
                    if v in ("baseline", "oracle"):
                        guess, margin = (None, 0.0) if v == "baseline" else (target, 1.0)
                    else:
                        method, raw = v.split("@")
                        threshold = float(raw)
                        if method == "D":
                            guess, margin = choose(signals["B"], threshold)
                            if guess is None:
                                guess, margin = choose(signals["C"], threshold)
                        else:
                            guess, margin = choose(signals[method], threshold)
                    predictions[v].append({"case_id": case["id"], "gold_project": target,
                                           "guess": guess, "margin": margin})
                    grades[v].append(grade_case(grading_case, "now", v, boost(pool, guess, proj_of, args.k), paths, {}, None))
                print(f"{i}/{len(cases)} {case['id']} pool={len(pool)}", flush=True)
        finally:
            await mg.close_graphiti()
    after = counts(args.graph)
    summaries = {}
    for v in variants:
        ps = predictions[v]
        regressions = [g.case_id for g, b in zip(grades[v], grades["baseline"])
                       if (g.memory_rank or args.k+1) > (b.memory_rank or args.k+1)]
        summaries[v] = {**summarize(grades[v], args.k), **inference_metrics(ps),
            "regressed_cases": regressions}
    result = {"graph": args.graph, "k": args.k, "requested_pool": args.pool, "pool_sizes": pool_sizes,
              "gold_source": args.gold_source or args.graph, "member_to_rollup": mapping,
              "classification_denominator": "Accuracy and confident-wrong use cases with a gold episode project; guesses on unlabeled cases are reported separately. Abstention uses all cases.",
              "added_signal_cost_ms": {m:{'mean':sum(values)/len(values),'max':max(values)} for m,values in costs_ms.items()},
              "margin_definition": "(top - runner_up) / sum(positive scores); strictly > threshold",
              "entity_lookup": "offline name dictionary and project sizes; one matched-entity read per query; no gold used",
              "counts_before": before, "counts_after": after, "counts_unchanged": before == after,
              "summaries": summaries, "predictions": predictions,
              "grades": {v: [g.as_dict() for g in gs] for v, gs in grades.items()}}
    out.write_text(json.dumps(result, indent=2) + "\n")
    lines = [f"B16 inference — {args.graph}", "", "| Method | Hit@8 | MRR | Wiki@8 | Accuracy guessed | Abstain | Wrong/labeled | Unlabeled guesses | Regressed IDs |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    def fmt(x):
        return '—' if x is None else f'{x:.4f}'
    for v, s in summaries.items():
        lines.append('| ' + ' | '.join([v] + [fmt(s[key]) for key in
            ['memory_hit_at_k', 'memory_mrr', 'wiki_hit_at_k', 'accuracy_when_guessed', 'abstain_rate', 'confident_wrong_rate']]
            + [str(len(s['guesses_without_gold_project'])), ', '.join(s['regressed_cases']) or '—']) + ' |')
    lines += ["", f"Requested pool {args.pool}; actual sizes {dict(Counter(pool_sizes))}.",
              "E reuses embeddings from recall; no extra embedding calls.",
              result['classification_denominator'],
              "Threshold sweep is exploratory on this eval set, not independent validation."]
    out.with_suffix('.md').write_text('\n'.join(lines) + '\n')
    print('\n'.join(lines), flush=True)
    return 0 if before == after else 3


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--graph', default='mem-fabric-local')
    ap.add_argument('--gold-source', help='read-only original graph for resolving stable gold memory IDs after merge')
    ap.add_argument('--rollups', help='local validated roll-up JSONL for explicit gold lineage mapping')
    ap.add_argument('--pool', type=int, default=24)
    ap.add_argument('--k', type=int, default=8)
    ap.add_argument('--thresholds', nargs='+', type=float, default=[0, .1, .2, .3])
    ap.add_argument('--out', default=str(ROOT / 'imports/replay/b16-inference-comparison-20261013.json'))
    args = ap.parse_args()
    if args.k <= 0 or args.pool < args.k or any(t < 0 or t > 1 for t in args.thresholds):
        ap.error('Require pool >= k > 0 and thresholds in [0, 1]')
    return asyncio.run(run(args))


if __name__ == '__main__':
    raise SystemExit(main())
