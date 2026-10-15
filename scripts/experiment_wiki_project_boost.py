"""Offline wiki-side project boost (B16, 2026-10-14). Read-only; no LLM, no embeddings.

Takes the project guesses already saved by experiment_project_inference.py
(A, then E when A abstains), runs the replay's lexical wiki search for a wider
pool, moves results whose path maps to the guessed project
(their `project:` frontmatter, server/wiki_projects.py) to the front, and grades the top k against each
case's gold wiki notes. Outputs stay in the ignored imports/replay/ and carry
case IDs and numbers only.

    uv run python scripts/experiment_wiki_project_boost.py \
        --predictions imports/replay/b16-inference-postb17-20261014.json
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def boost_paths(paths: list[str], guess: str | None, project_of, k: int) -> list[str]:
    """Stable partition: paths in the guessed project first, then the rest; top k."""
    if not guess:
        return paths[:k]
    inside = [p for p in paths if project_of(p) == guess]
    return (inside + [p for p in paths if project_of(p) != guess])[:k]


def first_rank(paths: list[str], gold: set[str]) -> int | None:
    return next((i for i, p in enumerate(paths, 1) if p in gold), None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--predictions", required=True, help="experiment_project_inference.py output JSON")
    ap.add_argument("--labels", default=str(ROOT / "tests/fixtures/ms7_eval/b16-project-labels.json"))
    ap.add_argument("--first", default="A@0.1")
    ap.add_argument("--fallback", default="E@0.001")
    ap.add_argument("--pool", type=int, default=24)
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--out", default=str(ROOT / "imports/replay/b16-wiki-boost-20261014.json"))
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from server.replay.runner import _WikiIndex
    from server.wiki_projects import project_for_file

    out = Path(args.out)
    if not out.resolve().is_relative_to((ROOT / "imports/replay").resolve()):
        raise SystemExit("Outputs must stay in the ignored imports/replay directory")
    wiki_root = Path(os.environ["LLM_WIKI_PATH"]).expanduser()
    project_of = lambda p: project_for_file(wiki_root, p)  # noqa: E731

    preds = json.loads(Path(args.predictions).read_text())["predictions"]
    first = {p["case_id"]: p for p in preds[args.first]}
    fallback = {p["case_id"]: p for p in preds[args.fallback]}
    labels = {l["case_id"]: l["label"] for l in json.loads(Path(args.labels).read_text())["labels"]}
    cases = [c for c in json.loads((ROOT / "tests/fixtures/ms7_eval/queries.json").read_text())["queries"]
             if c.get("gold_wiki")]
    wiki = _WikiIndex(Path(os.environ["LLM_WIKI_PATH"]).expanduser())

    variants = {"baseline": {}, "A->E": {}, "oracle": {}}
    for c in cases:
        cid, gold = c["id"], set(c["gold_wiki"])
        pool = [r.relative_path for r in wiki.search(c["query"], args.pool)]
        guess = first[cid]["guess"] or fallback[cid]["guess"]
        label = labels.get(cid, first[cid]["gold_project"])
        oracle = None if label in (None, "ai-research") else label
        for name, g in (("baseline", None), ("A->E", guess), ("oracle", oracle)):
            ranked = boost_paths(pool, g, project_of, args.k)
            variants[name][cid] = {"guess": g, "rank": first_rank(ranked, gold),
                                   "gold_in_pool": first_rank(pool, gold)}

    def summary(rows):
        n = len(rows)
        base = variants["baseline"]
        return {"cases": n,
                "wiki_hit_at_k": sum(1 for r in rows.values() if r["rank"]) / n,
                "wiki_mrr": sum(1 / r["rank"] for r in rows.values() if r["rank"]) / n,
                "improved": [c for c, r in rows.items() if (r["rank"] or 99) < (base[c]["rank"] or 99)],
                "regressed": [c for c, r in rows.items() if (r["rank"] or 99) > (base[c]["rank"] or 99)]}

    result = {"pool": args.pool, "k": args.k, "guess": f"{args.first} then {args.fallback}",
              "summaries": {v: summary(rows) for v, rows in variants.items()}, "cases": variants}
    out.write_text(json.dumps(result, indent=1) + "\n")
    lines = [f"Wiki-side project boost — {len(cases)} cases with gold wiki notes, pool {args.pool}, k {args.k}", "",
             "| Variant | Wiki@8 | Wiki MRR | Improved | Regressed |", "|---|---|---|---|---|"]
    for v, s in result["summaries"].items():
        lines.append(f"| {v} | {s['wiki_hit_at_k']:.3f} | {s['wiki_mrr']:.3f} | {' '.join(s['improved']) or '—'} | {' '.join(s['regressed']) or '—'} |")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
