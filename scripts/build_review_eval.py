#!/usr/bin/env python3
"""Build the local review-judge eval set from the journal's review history.

Read-only against the journal and the proposal folders. Writes JSONL rows (one
per recommendation) to an output folder that must stay OUTSIDE git: it holds
real episode statements and doc text. Nothing here is ever printed except
counts. No model is called; flag/reject causes are keyword tags on the
recorded reason.

Run where the journal lives (inside cmf-server, since the journal sits under
/data/cmf):

    docker exec -i cmf-server python - < scripts/build_review_eval.py

or locally with --journal / --proposals-dir / --out.

Splits are by the date the nightly run started (UTC), never random:
    train  < 2026-11-09
    dev    2026-11-09 .. 2026-11-12
    test   >= 2026-11-13   (the Nov 12-15 nightly runs)
Rows with no usable label (skipped, closed, still pending with no final
verdict) get split "unlabeled" except that the test split keeps pending rows,
since those are scored once the verdicts land.

Doc labels before 2026-11-12 treat flag and reject as one class: the
"fixable docs are flagged, not rejected" rule only arrived that day, so
`target_binary` (approve vs not_approve) is the comparable label.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

DEV_START = "2026-11-09"
TEST_START = "2026-11-13"
DEFAULT_JOURNAL = "/data/cmf/imports/journal/journal.db"
DEFAULT_PROPOSALS = "/data/cmf/proposals-docs"
STATEMENT_LIMIT = 600

# First match wins. Tags describe why a non-approve was recorded, from the
# reason text only.
CAUSES = [
    ("project_tag", r"\bproject\b.*\b(tag|slug|retag|wrong|missing)|retag|wrong project|project tag|worktree"),
    ("doc_shape", r"frontmatter|front matter|heading|doubled|repeat|duplicated body|body (is )?(twice|repeated)|WIKI/|after related|after sources|malformed|title"),
    ("duplicate", r"duplicate|dup of|same as|already (covered|captured|approved)|superseded by"),
    ("narration", r"narrat|process|investigat|working on|is looking|status update|progress"),
    ("chatter_draft", r"chatter|draft|casual|small talk|throwaway|transient|one-off|ephemeral|boilerplate"),
    ("open_question", r"question|unanswered|no answer|unresolved|waiting|wait for"),
    ("misattribution", r"misattribut|attribut|wrong person|not the user|someone else"),
    ("third_party_or_sensitive", r"third.party|other people|private|financial|legal|medical|salary|sensitive|personal detail"),
    ("churn", r"churn|stale|outdated|changed since|superseded|obsolete"),
]


def cause_of(reason: str | None) -> str:
    text = (reason or "").lower()
    for tag, pattern in CAUSES:
        if re.search(pattern, text):
            return tag
    return "other"


def split_for(run_date: str, status: str, final: str | None) -> str:
    if run_date >= TEST_START:
        return "test"
    if final not in ("approve", "reject", "flag"):
        return "unlabeled"
    if run_date >= DEV_START:
        return "dev"
    return "train"


def load_doc(proposals_dir: Path, proposal_id: str) -> dict | None:
    for sub in ("", "approved", "rejected", "applied"):
        path = proposals_dir / sub / f"{proposal_id}.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    return None


def clip(text: str | None, limit: int = STATEMENT_LIMIT) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + "…"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--journal", default=DEFAULT_JOURNAL)
    ap.add_argument("--proposals-dir", default=DEFAULT_PROPOSALS)
    ap.add_argument("--out", default=None, help="default: <journal>/../../review_eval")
    args = ap.parse_args(sys.argv[1:])

    journal = Path(args.journal)
    out = Path(args.out) if args.out else journal.parent.parent / "review_eval"
    out.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{journal}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    runs = {r["run_id"]: r for r in conn.execute("SELECT run_id, started_at FROM review_runs")}
    recs = conn.execute(
        "SELECT * FROM review_recommendations ORDER BY run_id, item_type DESC, "
        "CAST(SUBSTR(label, 3) AS INTEGER), label"
    ).fetchall()

    derived = {
        r["memory_id"]: r
        for r in conn.execute(
            "SELECT memory_id, policy_version, category, statement, reason, confidence, event_date, "
            "date_precision, reasoning_kind, thread_key, project, evidence_event_ids_json FROM derived_memories"
        )
    }

    proposals_dir = Path(args.proposals_dir)
    peers: dict[str, list[dict]] = {}
    rows: list[dict] = []
    missing = Counter()

    for r in recs:
        run = runs.get(r["run_id"])
        run_date = (run["started_at"] if run else r["created_at"])[:10]
        final = r["final_verdict"]
        row = {
            "run_id": r["run_id"],
            "run_date": run_date,
            "label": r["label"],
            "item_type": r["item_type"],
            "item_id": r["item_id"],
            "recommendation": r["verdict"],
            "reason": r["reason"],
            "status": r["status"],
            "final_verdict": final,
            "split": split_for(run_date, r["status"], final),
        }
        # approve vs not_approve is the label comparable across the rule change
        row["target_binary"] = None if final is None else ("approve" if final == "approve" else "not_approve")
        row["cause"] = cause_of(r["reason"]) if r["verdict"] != "approve" or final in ("reject", "flag") else None

        if r["item_type"] == "episode":
            d = derived.get(r["item_id"])
            if d is None:
                missing["episode_no_derived_row"] += 1
                row["item"] = None
            else:
                row["item"] = {
                    "statement": clip(d["statement"]),
                    "kind": d["reasoning_kind"],
                    "project": d["project"],
                    "event_date": d["event_date"],
                    "date_precision": d["date_precision"],
                    "thread_key": d["thread_key"],
                    "confidence": d["confidence"],
                    "policy_version": d["policy_version"],
                    "rationale": clip(d["reason"], 400),
                    "evidence_turns": len(json.loads(d["evidence_event_ids_json"] or "[]")),
                    "harness": r["item_id"].split(":")[1] if r["item_id"].count(":") > 2 else None,
                }
        else:
            p = load_doc(proposals_dir, r["item_id"])
            if p is None:
                missing["doc_no_proposal_file"] += 1
                row["item"] = None
            else:
                row["item"] = {
                    "operation": p.get("operation"),
                    "target_path": p.get("target_path"),
                    "mode": p.get("mode"),
                    "target_heading": p.get("target_heading"),
                    "addition": clip(p.get("addition"), 1500),
                    "rationale": clip(p.get("rationale")),
                    "project": p.get("source_project"),
                    "source_date": p.get("source_date"),
                    "conflicts_with": p.get("conflicts_with"),
                    "proposed_chars": len(p.get("proposed_content") or ""),
                }
        peers.setdefault(r["run_id"], []).append(row)
        rows.append(row)

    # Stand-in for the nightly duplicate check: earlier items (previous runs) on the same thread.
    seen_by_thread: dict[str, list[dict]] = {}
    for row in sorted(rows, key=lambda x: (x["run_date"], x["run_id"])):
        it = row.get("item") or {}
        tk = it.get("thread_key")
        if row["item_type"] == "episode" and tk:
            prior = [x for x in seen_by_thread.get(tk, []) if x["run_id"] != row["run_id"]][-3:]
            it["earlier_same_thread"] = [clip(x["statement"], 200) for x in prior]
            seen_by_thread.setdefault(tk, []).append({"run_id": row["run_id"], "statement": it.get("statement")})

    # What else the judge saw in the same batch (for duplicate calls).
    for run_rows in peers.values():
        for row in run_rows:
            row["batch_peers"] = [
                {"label": o["label"], "statement": (o["item"] or {}).get("statement") or (o["item"] or {}).get("rationale")}
                for o in run_rows if o is not row
            ]

    by_split: dict[str, list[dict]] = {}
    for row in rows:
        by_split.setdefault(row["split"], []).append(row)
    out.joinpath("all.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
    for name, items in by_split.items():
        out.joinpath(f"{name}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in items), encoding="utf-8")

    decided = [x for x in rows if x["final_verdict"] in ("approve", "reject", "flag")]
    print(f"rows: {len(rows)}   decided: {len(decided)}   written to: {out}")
    print("splits:", dict(Counter(x["split"] for x in rows)))
    print("by type/status:", dict(Counter((x["item_type"], x["status"]) for x in rows)))
    print("recommendation vs final (decided):", dict(Counter((x["recommendation"], x["final_verdict"]) for x in decided)))
    print("causes (non-approve):", dict(Counter(x["cause"] for x in rows if x["cause"])))
    print("missing item text:", dict(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
