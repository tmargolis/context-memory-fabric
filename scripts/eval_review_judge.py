#!/usr/bin/env python3
"""Score candidate review judges against Todd's recorded verdicts (Phase 2).

Local only. Reads the eval rows written by scripts/build_review_eval.py and
writes model outputs under <eval dir>/results/, which stays outside git. Only
counts and metrics are printed; no item text, no keys.

Each model judges one nightly run per call, with the same rules text the
nightly judge gets (generic rules + the operator's own), as production does.
Models are called directly (never through the failover chain) so a result
always belongs to the model named.

Usage:
    # prove the scorer on the stored nightly recommendations (no model calls)
    python scripts/eval_review_judge.py --score-stored

    # estimate tokens and cost per candidate; makes NO API calls
    python scripts/eval_review_judge.py --dry-run --models anthropic:claude-sonnet-5-5 ...

    # run a candidate (provider:model; provider in anthropic|openai|gemini|local)
    python scripts/eval_review_judge.py --models anthropic:claude-haiku-5-5 --split dev,test

API keys come from ~/.cmf/.env (ANTHROPIC_API_KEY, OPENAI_API_KEY,
GEMINI_API_KEY) and are never printed. Prices for --dry-run come from
--prices (JSON: {"provider:model": [usd_per_mtok_in, usd_per_mtok_out]}).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RULES_PATH = REPO / "server" / "review" / "review_rules.md"
STATE = Path(os.environ.get("CMF_STATE_DIR", Path.home() / ".cmf" / "state"))
EVAL_DIR = STATE / "imports" / "review_eval"
ENV_FILE = Path.home() / ".cmf" / ".env"
PEER_CAP = 0  # items in one call already include each other; peers are not repeated
SPLITS_DEFAULT = ("train", "dev", "test")

SYSTEM = (
    "You are the nightly review judge for Context Memory Fabric. For each item decide "
    "approve, reject or flag, following the rules below. Answer with ONLY a JSON array, one "
    'object per item: {"label": "EP1", "verdict": "approve|reject|flag", "reason": "<under 20 words>"}. Keep every reason short; output nothing but the array.\n\n'
    "# Rules\n\n"
)


def rules_text() -> str:
    text = RULES_PATH.read_text(encoding="utf-8").strip()
    mine = Path(os.environ.get("CMF_REVIEW_RULES_PATH", STATE / "review-rules.md"))
    if mine.is_file():
        text += "\n\n## Your own rules\n\n" + mine.read_text(encoding="utf-8").strip()
    return text


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


def load_rows(splits: tuple[str, ...]) -> list[dict]:
    rows = []
    for line in (EVAL_DIR / "all.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        rows.append(row)
    return rows


def render_items(run_rows: list[dict]) -> str:
    """The items as the judge saw them: no recommendation, no final verdict."""
    items = []
    for r in run_rows:
        item = dict(r.get("item") or {})
        item.update(label=r["label"], item_type=r["item_type"])
        items.append(item)
    return json.dumps(items, ensure_ascii=False)


def parse_verdicts(text: str) -> dict[str, dict]:
    """Parse the JSON array; if it was cut off, keep every complete object before the cut."""
    match = re.search(r"\[.*\]", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            return {str(d.get("label")): d for d in data if isinstance(d, dict)}
        except json.JSONDecodeError:
            pass
    out = {}
    for obj in re.findall(r"\{[^{}]*\}", text):
        try:
            d = json.loads(obj)
        except json.JSONDecodeError:
            continue
        if "label" in d and "verdict" in d:
            out[str(d["label"])] = d
    return out


# --- scoring ----------------------------------------------------------------------


def score(pairs: list[tuple[dict, str | None]]) -> dict:
    """pairs: (row, judge_verdict). Only rows with a final verdict count.

    agree        judge == final, where final is approve/reject
    false_approve  judge approved something Todd did not approve (the costly error)
    flag_rate    share of items the judge flagged (a flag is not counted wrong)
    binary_agree approve-vs-not-approve agreement (comparable across the Nov 12 rule change)
    """
    out: dict[str, dict] = {}
    for scope in ("episode", "doc", "all"):
        sel = [(r, v) for r, v in pairs if r["final_verdict"] and (scope == "all" or r["item_type"] == scope)]
        n = len(sel)
        if not n:
            continue
        decided = [(r, v) for r, v in sel if r["final_verdict"] in ("approve", "reject")]
        out[scope] = {
            "n": n,
            "agree": round(sum(v == r["final_verdict"] for r, v in decided) / max(len(decided), 1), 4),
            "false_approve": sum(v == "approve" and r["final_verdict"] != "approve" for r, v in sel),
            "missed_reject_or_flag": sum(v != "approve" and r["final_verdict"] == "approve" for r, v in sel),
            "flag_rate": round(sum(v == "flag" for r, v in sel) / n, 4),
            "binary_agree": round(sum(
                (v == "approve") == (r["final_verdict"] == "approve") for r, v in sel) / n, 4),
            "no_answer": sum(v is None for r, v in sel),
        }
    return out


# --- providers --------------------------------------------------------------------


def post(url: str, headers: dict, body: dict, timeout: int = 900) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:  # never echo headers (they hold keys)
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode()[:300]}") from None


def call(provider: str, model: str, system: str, user: str, env: dict) -> tuple[str, int, int]:
    """Returns (text, input_tokens, output_tokens)."""
    if provider == "anthropic":
        r = post("https://api.anthropic.com/v1/messages",
                 {"x-api-key": env["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
                 {"model": model, "max_tokens": 16000, "system": system, "messages": [{"role": "user", "content": user}]})
        text = "".join(b.get("text", "") for b in r["content"])
        return text, r["usage"]["input_tokens"], r["usage"]["output_tokens"]
    if provider in ("openai", "local"):
        base = "https://api.openai.com/v1" if provider == "openai" else env.get("CMF_LOCAL_BASE_URL", "http://127.0.0.1:12345/v1")
        key = env.get("OPENAI_API_KEY") if provider == "openai" else env.get("CMF_LOCAL_API_KEY", "lm-studio")
        r = post(f"{base}/chat/completions", {"authorization": f"Bearer {key}"},
                 {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
                 timeout=1800 if provider == "local" else 300)
        u = r.get("usage", {})
        return r["choices"][0]["message"]["content"] or "", u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
    if provider == "gemini":
        r = post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                 {"x-goog-api-key": env["GEMINI_API_KEY"]},
                 {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}]})
        text = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
        u = r.get("usageMetadata", {})
        return text, u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0)
    raise SystemExit(f"unknown provider {provider!r}")


# --- main -------------------------------------------------------------------------


def group_runs(rows: list[dict], splits: tuple[str, ...]) -> dict[str, list[dict]]:
    runs: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        runs[r["run_id"]].append(r)
    # keep a run if any of its rows is in a requested split
    return {rid: rs for rid, rs in runs.items() if any(r["split"] in splits for r in rs)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--models", default="", help="comma list of provider:model")
    ap.add_argument("--split", default=",".join(SPLITS_DEFAULT))
    ap.add_argument("--chunk-items", type=int, default=0, help="judge each run in chunks of N items (for small-context local models)")
    ap.add_argument("--tag", default="", help="suffix for result files (keeps runs on different eval rows apart)")
    ap.add_argument("--pause", type=float, default=0.5, help="seconds between calls (raise for rate-limited models)")
    ap.add_argument("--dry-run", action="store_true", help="estimate tokens/cost only; no API calls")
    ap.add_argument("--score-stored", action="store_true", help="score the nightly judge's stored recommendations")
    ap.add_argument("--prices", default=None, help="JSON file {provider:model: [in, out]} USD per million tokens")
    args = ap.parse_args()

    splits = tuple(s for s in args.split.split(",") if s)
    rows = load_rows(splits)
    runs = group_runs(rows, splits)
    scored_rows = [r for r in rows if r["split"] in splits]

    if args.score_stored:
        pairs = [(r, r["recommendation"]) for r in scored_rows]
        print("nightly judge (stored):", json.dumps(score(pairs)))
        by_split = {s: score([(r, r["recommendation"]) for r in scored_rows if r["split"] == s]).get("all") for s in splits}
        print("by split:", json.dumps(by_split))
        return 0

    system = SYSTEM + rules_text()
    models = [m for m in args.models.split(",") if m]
    prices = json.loads(Path(args.prices).read_text()) if args.prices else {}

    if args.dry_run:
        # ~4 characters per token; output ~60 tokens per item
        in_tok = sum((len(system) + len(render_items(rs))) // 4 for rs in runs.values())
        out_tok = sum(60 * len(rs) for rs in runs.values())
        print(f"runs: {len(runs)}  items: {sum(len(rs) for rs in runs.values())}")
        print(f"estimated tokens per model: in {in_tok:,}  out {out_tok:,}")
        for m in models or prices:
            p = prices.get(m)
            cost = f"${in_tok / 1e6 * p[0] + out_tok / 1e6 * p[1]:.2f}" if p else "price unknown"
            print(f"  {m}: {cost}")
        return 0

    env = load_env()
    results_dir = EVAL_DIR / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    for spec in models:
        provider, model = spec.split(":", 1)
        out_file = results_dir / (re.sub(r"[^A-Za-z0-9._-]", "_", spec) + args.tag + ".jsonl")
        done: dict[str, dict] = {}
        if out_file.is_file():  # resume: skip runs already answered
            for line in out_file.read_text().splitlines():
                rec = json.loads(line)
                done[rec["run_id"]] = rec
        t_in = t_out = 0
        with out_file.open("a") as fh:
            for rid, rs in sorted(runs.items()):
                if rid in done:
                    continue
                verdicts, i, o = {}, 0, 0
                n = args.chunk_items
                chunks = [rs[k:k + n] for k in range(0, len(rs), n)] if n else [rs]
                for chunk in chunks:
                    for attempt in range(5):
                        try:
                            text, ci, co = call(provider, model, system, render_items(chunk), env)
                        except (TimeoutError, OSError, RuntimeError):
                            if attempt == 4:
                                raise
                            time.sleep(30 * (attempt + 1))  # 503 / rate limits: back off
                            continue
                        if parse_verdicts(text) or attempt == 4:
                            break
                        time.sleep(2)
                    verdicts.update(parse_verdicts(text))
                    i += ci
                    o += co
                    if len(chunks) > 1:
                        time.sleep(args.pause)
                rec = {"run_id": rid, "in": i, "out": o, "verdicts": verdicts, "parsed": bool(verdicts)}
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                done[rid] = rec
                t_in += i
                t_out += o
                time.sleep(args.pause)
        pairs = []
        for rid, rs in runs.items():
            v = done[rid]["verdicts"]
            for r in rs:
                if r["split"] in splits:
                    pairs.append((r, (v.get(r["label"]) or {}).get("verdict")))
        tot_in = sum(d["in"] for d in done.values())
        tot_out = sum(d["out"] for d in done.values())
        print(spec, json.dumps(score(pairs)), f"tokens in {tot_in:,} out {tot_out:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
