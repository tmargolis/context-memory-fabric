#!/usr/bin/env python3
"""Tune the review judge's prompt with DSPy, offline (Phase 3). Never imported by the server.

The judge is one dspy.Predict module that sees ONE item at a time (plus short context
from the same night) and answers approve / reject / flag. Its starting instructions are
the review rules text, exactly what the nightly judge reads. DSPy then searches for
better few-shot examples (BootstrapFewShot) or instructions (MIPROv2) against Todd's
recorded verdicts. The tuned prompt is a plain artifact under ~/.cmf/state/review-judge/
(outside git; it contains real items as examples).

Splits are by date and come from the eval rows (scripts/build_review_eval.py):
    train  < 2026-11-09 (tuning)   dev  Nov 9-12   test  Nov 13 on
dev and test are never used for tuning; they score the baseline and the tuned program.

dspy lives in the optional `tune` dependency group (pyproject.toml); the server image never installs it.
Usage:
    uv run --group tune python scripts/tune_review_judge.py --mode baseline
    uv run --group tune python scripts/tune_review_judge.py --mode bootstrap
    uv run --group tune python scripts/tune_review_judge.py --mode mipro
Add --no-flag to judge approve/reject only (no human flag step).

Only counts, metrics and token totals are printed. The model is called directly
(default anthropic/claude-haiku-5-5); the key comes from ~/.cmf/.env and is not printed.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_review_judge as E  # noqa: E402  (rules text, row loading, scorer)

OUT_DIR = E.STATE / "review-judge"
OUT_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("DSPY_CACHEDIR", str(OUT_DIR / "cache"))  # keep cached prompts with the other private state

import dspy  # noqa: E402

PRICES = {"claude-haiku-5-5": (0.10, 0.50)}  # USD per million tokens, in / out (prompts <= 100K)
PEERS = 8
PEER_CLIP = 110


class JudgeItem(dspy.Signature):
    """placeholder; replaced by the review rules below"""

    item_type: str = dspy.InputField(desc="EP (a staged episode) or DOC (a doc proposal)")
    item: str = dspy.InputField(desc="the item's fields as JSON")
    same_night: str = dspy.InputField(desc="short statements of the other items judged the same night")
    verdict: str = dspy.OutputField(desc="exactly one of: approve, reject, flag")
    reason: str = dspy.OutputField(desc="a short reason, under 20 words")


NO_FLAG = (
    "There is no flag verdict. Nobody will review your decision, so every item must be approve or reject. "
    "Wherever the rules below say to flag, decide instead: approve when the item stands as written, or when "
    "the only problem is a wrong project slug (put the right slug in the reason); reject when the content is "
    "not worth keeping, or when part is good and part is bad, in which case start the reason with 'rebuild:' "
    "and say what to keep and what to drop or fix. Never answer flag.\n\n"
)
NO_FLAG_MODE = False


def instructions() -> str:
    if NO_FLAG_MODE:
        head = ("You are the nightly review judge for Context Memory Fabric. Judge the ONE item below as "
                "approve or reject, following the rules. Answer verdict as exactly one of those two words.\n\n" + NO_FLAG)
    else:
        head = ("You are the nightly review judge for Context Memory Fabric. Judge the ONE item below as "
                "approve, reject or flag, following the rules. Answer verdict as exactly one of those words.\n\n")
    return head + "# Rules\n\n" + E.rules_text()


class Judge(dspy.Module):
    def __init__(self) -> None:
        super().__init__()
        self.predict = dspy.Predict(JudgeItem.with_instructions(instructions()))

    def forward(self, item_type: str, item: str, same_night: str):
        return self.predict(item_type=item_type, item=item, same_night=same_night)


def examples(rows: list[dict]) -> list[dspy.Example]:
    by_run: dict[str, list[dict]] = {}
    for r in rows:
        by_run.setdefault(r["run_id"], []).append(r)
    out = []
    for r in rows:
        if r["final_verdict"] not in ("approve", "reject", "flag") or r.get("item") is None:
            continue
        item = dict(r["item"])
        item.update(label=r["label"])
        peers = [
            (p.get("statement") or "")[:PEER_CLIP].replace("\n", " ")
            for p in r.get("batch_peers", [])[:PEERS]
        ]
        ex = dspy.Example(
            item_type="EP" if r["item_type"] == "episode" else "DOC",
            item=json.dumps(item, ensure_ascii=False),
            same_night=" | ".join(f"{p['label']}: {(p.get('statement') or '')[:PEER_CLIP]}".replace("\n", " ")
                                  for p in r.get("batch_peers", [])[:PEERS]),
            final=r["final_verdict"], row_id=f"{r['run_id']}/{r['label']}",
        ).with_inputs("item_type", "item", "same_night")
        out.append(ex)
    return out


def metric(example, pred, trace=None) -> float:
    """1 exact; a false approve (approving what Todd did not approve) is 0; a flag in place of a
    decision costs him a read, so partial credit; flag-final docs accept a reject (pre-Nov-12 convention)."""
    v = (getattr(pred, "verdict", "") or "").strip().lower()
    final = example.final
    if NO_FLAG_MODE:  # decisions only: a flag or any other answer scores 0; a wrong approve is the costly error
        if v == final:
            return 1.0
        return 0.3 if (v == "reject" and final == "approve") else 0.0
    if v == final:
        return 1.0
    if v == "approve":
        return 0.0
    if final == "flag":
        return 0.7 if v == "reject" else 0.0
    if v == "flag":
        return 0.4
    return 0.0  # rejected something he approved


def run_program(program, exs, threads: int) -> list[tuple[dspy.Example, str | None]]:
    def one(ex):
        try:
            p = program(item_type=ex.item_type, item=ex.item, same_night=ex.same_night)
            return ex, (p.verdict or "").strip().lower()
        except Exception as exc:  # one bad call must not sink the run
            print("call failed:", type(exc).__name__, str(exc)[:120], file=sys.stderr)
            return ex, None

    with ThreadPoolExecutor(max_workers=threads) as pool:
        return list(pool.map(one, exs))


def report(name: str, preds, rows_by_id: dict) -> dict:
    pairs = [(rows_by_id[ex.row_id], (None if NO_FLAG_MODE and v not in ("approve", "reject") else v)) for ex, v in preds]
    s = E.score(pairs)
    a = s["all"]
    print(f"{name:28} n={a['n']:3} agree {a['agree']:.3f}  approve-vs-not {a['binary_agree']:.3f}  "
          f"FA {a['false_approve']:2}  flag {a['flag_rate']*100:3.0f}%  no-answer {a['no_answer']}  "
          f"| episodes agree {s['episode']['agree']:.3f} FA {s['episode']['false_approve']}"
          + (f" | docs agree {s['doc']['agree']:.3f} FA {s['doc']['false_approve']}" if "doc" in s else ""))
    return s


def usage_cost(lm, model: str) -> str:
    tin = tout = 0
    for h in lm.history:
        u = h.get("usage") or {}
        tin += u.get("prompt_tokens", 0) or 0
        tout += u.get("completion_tokens", 0) or 0
    pi, po = PRICES.get(model.split("/")[-1], (0, 0))
    return f"tokens in {tin:,} out {tout:,} (about ${tin/1e6*pi + tout/1e6*po:.2f})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=["baseline", "bootstrap", "mipro"], required=True)
    ap.add_argument("--model", default="anthropic/claude-haiku-5-5")
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--train-n", type=int, default=120, help="train items used for tuning")
    ap.add_argument("--val-n", type=int, default=60, help="train items held back to pick between candidates (mipro)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-flag", action="store_true",
                    help="approve or reject only (no flag verdict), to remove the human review step")
    args = ap.parse_args()
    global NO_FLAG_MODE
    NO_FLAG_MODE = args.no_flag

    env = E.load_env()
    # DSPy 3.5's own Anthropic client needs the /v1 base explicitly. Haiku 5.5 rejects non-default
    # sampling, so temperature stays at its default 1.0.
    lm = dspy.LM(args.model, api_key=env["ANTHROPIC_API_KEY"], api_base="https://api.anthropic.com/v1",
                 temperature=1.0, max_tokens=8000, cache=False)
    dspy.configure(lm=lm)

    rows = E.load_rows(("train", "dev", "test"))
    rows_by_id = {f"{r['run_id']}/{r['label']}": r for r in rows}
    split = lambda name: examples([r for r in rows if r["split"] == name])
    train, dev, test = split("train"), split("dev"), split("test")
    random.Random(args.seed).shuffle(train)
    print(f"train {len(train)}  dev {len(dev)}  test {len(test)}  (model {args.model})")

    judge = Judge()
    if args.mode == "baseline":
        pass
    elif args.mode == "bootstrap":
        opt = dspy.BootstrapFewShot(metric=metric, metric_threshold=0.99, max_bootstrapped_demos=3,
                                    max_labeled_demos=3, max_rounds=1, max_errors=20)
        judge = opt.compile(Judge(), trainset=train[: args.train_n])
    else:
        opt = dspy.MIPROv2(metric=metric, prompt_model=lm, task_model=lm, auto="light",
                           num_threads=args.threads, seed=args.seed, verbose=False)
        judge = opt.compile(Judge(), trainset=train[: args.train_n],
                            valset=train[args.train_n: args.train_n + args.val_n],
                            max_bootstrapped_demos=3, max_labeled_demos=3, requires_permission_to_run=False)

    stored = E.score([(r, r["recommendation"]) for r in rows if r["split"] in ("dev", "test") and r["final_verdict"]])["all"]
    print(f"{'stored nightly judge':28} n={stored['n']:3} agree {stored['agree']:.3f}  approve-vs-not {stored['binary_agree']:.3f}  "
          f"FA {stored['false_approve']:2}  flag {stored['flag_rate']*100:3.0f}%  (Opus 5.5, medium effort; its flags are not final verdicts)")
    for name, exs in (("dev", dev), ("test", test), ("dev+test", dev + test)):
        report(f"{args.mode} {name}", run_program(judge, exs, args.threads), rows_by_id)
    print(usage_cost(lm, args.model))
    if args.mode != "baseline":
        path = OUT_DIR / f"haiku-5-5-{'noflag-' if NO_FLAG_MODE else ''}{args.mode}.json"
        judge.save(str(path))
        print("saved tuned program to", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
