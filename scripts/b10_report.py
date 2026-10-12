"""B10 paired-replay report: extract@1.6 vs extract@1.7 on the same frozen inputs.

Reads a replay directory made by scripts/b10_replay.py (manifest.json,
review-runs.json, wiki/, 1.6/, 1.7/) and the operational journal and proposal
store read-only, and writes report.md + report.json under <out>/report/.
Every journal is read from a backup-API snapshot, never in place.

The waiting-run comparison is a review aid, not a verdict: each production
item is matched to replay output from its own source conversation.

  python scripts/b10_report.py --source <repo> --out <replay dir>
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import difflib
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHICAGO = ZoneInfo("America/Chicago")
MATCH, RETAIN = 0.55, 0.85  # statement similarity: same item / essentially unchanged


def _snapshot(db: Path, tmp: Path) -> sqlite3.Connection:
    dest = tmp / f"{abs(hash(str(db)))}.db"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    src.backup(dst)
    src.close()
    dst.row_factory = sqlite3.Row
    return dst


def _norm(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _parts(statement: str) -> list[str]:
    """A merged statement's numbered parts ("(1) ...; (2) ..."), or the statement itself."""
    parts = [x.strip(" ;") for x in re.split(r"(?:^|;\s*)\(\d+\)\s*", statement or "") if x.strip(" ;")]
    return parts or [statement or ""]


def _sim(a: str, b: str) -> float:
    """Best part-to-part similarity, so one window's episode matches the merge that absorbed it."""
    return max(difflib.SequenceMatcher(None, _norm(x), _norm(y), autojunk=False).ratio()
               for x in _parts(a) for y in _parts(b))


def _thread_of(row) -> str | None:
    if row["thread_key"]:
        return row["thread_key"]
    m = re.search(r"(?:^| \| )thread=([^|]+)", row["reason"] or "")
    return m.group(1).strip() if m else None


def _version_state(con: sqlite3.Connection, proposals_dir: Path) -> dict:
    """What a reviewer would see from one replay: queue, merges, threads, proposals."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    reviews = {r["memory_id"]: dict(r) for r in con.execute("SELECT * FROM reviews")} if "reviews" in tables else {}
    rows = [dict(r) for r in con.execute(
        "SELECT d.*, e.conversation_id, e.harness FROM derived_memories d "
        "LEFT JOIN events e ON e.event_id = d.source_event_id ORDER BY d.event_date, d.memory_id")]
    for r in rows:
        r["review_state"] = (reviews.get(r["memory_id"]) or {}).get("review_state", "pending")
        r["merged"] = r["memory_id"].startswith("reason:threadmerge:")
        r["thread"] = _thread_of(r)
    queue = [r for r in rows if r["approval_state"] == "queued_for_review" and r["review_state"] == "pending"]
    merges = [r for r in rows if r["merged"]]
    # A merged parent's children: rows of the same conversation whose evidence it absorbed.
    absorbed_pending = []
    for parent in merges:
        ev = set(json.loads(parent["evidence_event_ids_json"] or "[]"))
        for r in rows:
            if (not r["merged"] and r["conversation_id"] == parent["conversation_id"] and r["thread"]
                    and r["statement"] in parent["statement"]
                    and set(json.loads(r["evidence_event_ids_json"] or "[]")) <= ev
                    and r["review_state"] == "pending" and r["approval_state"] == "queued_for_review"):
                absorbed_pending.append((parent["memory_id"], r["memory_id"]))
    by_thread = defaultdict(lambda: {"conversations": set(), "projects": set(), "episodes": 0})
    for r in rows:
        if r["thread"] and not r["merged"]:
            t = by_thread[r["thread"]]
            t["conversations"].add(r["conversation_id"])
            t["projects"].add(r["project"])
            t["episodes"] += 1
    proposals = []
    for path in sorted(proposals_dir.rglob("*.json")):
        try:
            p = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if "proposal_id" in p:
            proposals.append(p)
    return {"rows": rows, "queue": queue, "merges": merges, "absorbed_pending": absorbed_pending,
            "threads": by_thread, "proposals": proposals}


def _frontmatter_dates(text: str) -> dict:
    m = re.match(r"\A---\s*\n(.*?)\n---", text or "", re.S)
    out = {}
    for key in ("created", "updated"):
        hit = re.search(rf"(?m)^{key}:\s*['\"]?(\d{{4}}-\d{{2}}-\d{{2}})", m.group(1)) if m else None
        out[key] = hit.group(1) if hit else None
    return out


def _doc_checks(proposals: list[dict], wiki: Path, events: dict, folders: set[str]) -> list[dict]:
    from server.eval_privacy import eval_leaks

    out = []
    for p in proposals:
        if p.get("status") == "rejected" and "superseded" in (p.get("review_notes") or ""):
            continue
        target = p.get("target_path", "")
        live = wiki / target
        conv_events = events.get(p.get("source_conversation_id"), [])
        days = sorted({e.astimezone(CHICAGO).date().isoformat() for e in conv_events})
        dates = _frontmatter_dates(p.get("proposed_content", ""))
        live_dates = _frontmatter_dates(live.read_text(encoding="utf-8", errors="replace")) if live.is_file() else {}
        parts = target.split("/")
        folder = "/".join(parts[:3]) if parts[:2] == ["WIKI", "projects"] else "/".join(parts[:2])
        check = {
            "proposal_id": p["proposal_id"], "target_path": target, "operation": p.get("operation"),
            "mode": p.get("mode", "legacy-replace"), "status": p.get("status"),
            "conversation": p.get("source_conversation_id"),
            "eval_leaks": eval_leaks(p.get("addition") or p.get("proposed_content", "")),
            "source_days_chicago": [days[0], days[-1]] if days else None,
            "created": dates["created"], "updated": dates["updated"],
            "folder_exists": folder in folders or not live.parent.exists() and parts[:1] == ["RAW"],
            "folder": folder,
        }
        if live.is_file():
            live_text = live.read_text(encoding="utf-8", errors="replace")
            check["live_body_preserved"] = (p.get("mode") == "append") and all(
                line in p.get("proposed_content", "") for line in live_text.splitlines() if line.strip()
                and not line.startswith("updated:"))
            check["created_kept"] = dates["created"] == live_dates.get("created")
            check["updated_not_regressed"] = (dates["updated"] or "") >= (live_dates.get("updated") or "")
        elif days:
            check["created_in_source_range"] = bool(dates["created"]) and days[0] <= dates["created"] <= days[-1]
            check["updated_in_source_range"] = bool(dates["updated"]) and days[0] <= dates["updated"] <= days[-1]
        out.append(check)
    return out


def _side_by_side(recs: list[dict], prod_con, source_root: Path, v16: dict, v17: dict, wiki: Path,
                  links: dict[str, str] | None = None) -> list[dict]:
    """Each waiting-run item against the same conversation's 1.6 and 1.7 replay output.

    `links` maps a capture-only conversation (a capture_session call journaled
    under its own cmf: id) to the transcript session that made the call, whose
    replay output it is then compared against.
    """
    links = links or {}
    groups = defaultdict(lambda: {"production": [], "docs": []})
    for rec in recs:
        if rec["item_type"] == "episode":
            row = prod_con.execute(
                "SELECT d.memory_id, d.statement, d.thread_key, d.reason, d.project, d.policy_version, e.conversation_id, e.harness "
                "FROM derived_memories d LEFT JOIN events e ON e.event_id = d.source_event_id WHERE d.memory_id = ?",
                (rec["item_id"],)).fetchone()
            if row is None:
                continue
            conv = row["conversation_id"]
            groups[conv]["production"].append({**dict(row), "run": rec["run_id"], "label": rec["label"],
                                               "recommendation": rec["verdict"], "summary": rec["summary"]})
        else:
            path = source_root / "proposals-docs" / f"{rec['item_id']}.json"
            p = json.loads(path.read_text()) if path.exists() else {}
            groups[p.get("source_conversation_id")]["docs"].append({**p, "run": rec["run_id"], "label": rec["label"],
                                                                     "recommendation": rec["verdict"]})
    report = []
    for conv, g in groups.items():
        source_conv = links.get(conv, conv)
        replay = {}
        for name, state in (("1.6", v16), ("1.7", v17)):
            replay[name] = [r for r in state["queue"] if r["conversation_id"] == source_conv]
        entry = {"conversation": conv, "capture_only": str(source_conv).startswith("cmf:"), "items": [],
                 "added_in_1_7": [], "docs": [], "linked_transcript": links.get(conv)}
        used = set()
        for item in g["production"]:
            best = {}
            for name in ("1.6", "1.7"):
                scored = sorted(((_sim(item["statement"], r["statement"]), r) for r in replay[name]),
                                key=lambda x: -x[0])
                best[name] = scored[0] if scored and scored[0][0] >= MATCH else (None, None)
            s17, r17 = best["1.7"]
            if r17 is not None:
                used.add(r17["memory_id"])
            outcome = ("capture-only (no transcript to replay)" if entry["capture_only"] else
                       "omitted" if r17 is None else "retained" if s17 >= RETAIN else "changed")
            entry["items"].append({
                "label": item["label"], "run": item["run"], "recommendation": item["recommendation"],
                "production": {"memory_id": item["memory_id"], "statement": item["statement"],
                               "thread": _thread_of(item), "project": item["project"]},
                "replay_1_6": best["1.6"][1] and {"statement": best["1.6"][1]["statement"], "similarity": round(best["1.6"][0], 2)},
                "replay_1_7": r17 and {"memory_id": r17["memory_id"], "statement": r17["statement"],
                                       "thread": r17["thread"], "similarity": round(s17, 2)},
                "outcome": outcome,
            })
        if conv not in links:  # a linked capture shares its session's output; additions stay with the session
            entry["added_in_1_7"] = [{"memory_id": r["memory_id"], "statement": r["statement"], "thread": r["thread"]}
                                     for r in replay["1.7"] if r["memory_id"] not in used]
        for doc in g["docs"]:
            mine = [p for p in v17["proposals"] if p.get("source_conversation_id") == conv
                    and p.get("status") != "rejected"]
            same = [p for p in mine if p.get("target_path") == doc.get("target_path")]
            entry["docs"].append({
                "label": doc["label"], "run": doc["run"], "recommendation": doc["recommendation"],
                "production": {"proposal_id": doc.get("proposal_id"), "target_path": doc.get("target_path"),
                               "operation": doc.get("operation"), "diff": doc.get("unified_diff", "")[:6000]},
                "replay_1_7": [{"proposal_id": p["proposal_id"], "target_path": p["target_path"], "mode": p.get("mode"),
                                "operation": p.get("operation"), "addition": p.get("addition"),
                                "diff": p.get("unified_diff", "")[:6000]} for p in (same or mine)],
                "outcome": ("capture-only (no transcript to replay)" if entry["capture_only"] else
                            "same target" if same else "different target" if mine else "omitted"),
            })
        report.append(entry)
    return report


def _rerun_merges(db: Path, tmp: Path, journal_rows: list[str]) -> dict:
    """Run 1.7 again over its own replay journal copy: every window is done, so only merges rerun."""
    from server.consolidation.pipeline import run_reasoning_consolidation
    from server.consolidation.store import ConsolidationStore
    from server.journal.store import SqliteEventStore
    from server.policies.extract import ExtractPolicyV1

    copy = tmp / "rerun.db"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    dst.close()

    def refuse(*args, **kwargs):
        raise AssertionError("rerun must not call the model")

    totals = Counter()
    with SqliteEventStore(copy) as js, ConsolidationStore(copy) as cs:
        for conv in journal_rows:
            stats = run_reasoning_consolidation(js, cs, ExtractPolicyV1(generate_fn=refuse), conversation_id=conv,
                                                wiki_root=tmp / "no-wiki", proposals_dir=tmp / "rerun-props")
            for key in ("windows_sent_to_model", "windows_failed", "episodes_merged_away", "threads_merged"):
                totals[key] += stats[key]
    con = sqlite3.connect(copy)
    con.row_factory = sqlite3.Row
    state = _version_state(con, tmp / "rerun-props")
    return {**totals, "absorbed_children_still_queued": len(state["absorbed_pending"])}


def build(source: Path, out: Path, links: dict[str, str] | None = None) -> dict:
    manifest = json.loads((out / "manifest.json").read_text())
    frozen = json.loads((out / "review-runs.json").read_text())
    wiki = out / "wiki"
    report_dir = out / "report"
    report_dir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        v16 = _version_state(_snapshot(out / "1.6" / "journal.db", tmp), out / "1.6" / "state" / "proposals-docs")
        v17 = _version_state(_snapshot(out / "1.7" / "journal.db", tmp), out / "1.7" / "state" / "proposals-docs")
        prod = _snapshot(source / "imports" / "journal" / "journal.db", tmp)

        src_con = sqlite3.connect(out / "source.db")
        events = defaultdict(list)
        from datetime import datetime
        for conv, at in src_con.execute("SELECT conversation_id, observed_at FROM events"):
            events[conv].append(datetime.fromisoformat(at))
        folders = {"/".join(p.relative_to(wiki).parts[:2]) for p in wiki.glob("*/*") if p.is_dir()}
        folders |= {"/".join(p.relative_to(wiki).parts[:3]) for p in (wiki / "WIKI" / "projects").glob("*") if p.is_dir()}

        def thread_metrics(state):
            per_conv = Counter(r["conversation_id"] for r in state["merges"])
            reuse = sorted(((k, len(v["conversations"]), sorted(p or "?" for p in v["projects"]), v["episodes"])
                            for k, v in state["threads"].items()), key=lambda x: (-x[1], -x[3], x[0]))
            cross = [{"thread": k, "conversations": n, "projects": projs, "episodes": e}
                     for k, n, projs, e in reuse if len(projs) > 1]
            return {
                "episodes": sum(not r["merged"] for r in state["rows"]),
                "queue": len(state["queue"]), "merges": len(state["merges"]),
                "merges_per_conversation": dict(per_conv),
                "threads": len(state["threads"]),
                "top5_reuse": [{"thread": k, "conversations": n, "projects": p, "episodes": e} for k, n, p, e in reuse[:5]],
                "cross_project_threads": cross,
                "merge_sample": [{"memory_id": m["memory_id"], "conversation": m["conversation_id"], "thread": m["thread"],
                                  "statement": m["statement"][:600]} for m in state["merges"][:10]],
                "absorbed_children_still_queued": len(state["absorbed_pending"]),
            }

        convs = [c["conversation_id"] for c in sorted(manifest["conversations"], key=lambda c: c["first"] or "")]
        result = {
            "baseline": manifest["baseline"], "model": manifest["model"],
            "conversations": len(manifest["conversations"]),
            "capture_only_conversations": [c["conversation_id"] for c in manifest["conversations"] if c["inference_windows"] == 0],
            "inference_windows_per_version": manifest["inference_windows_per_version"],
            "threads": {"1.6": thread_metrics(v16), "1.7": thread_metrics(v17)},
            "docs": {"1.6": _doc_checks(v16["proposals"], wiki, events, folders),
                     "1.7": _doc_checks(v17["proposals"], wiki, events, folders)},
            "rerun_1_7": _rerun_merges(out / "1.7" / "journal.db", tmp, convs),
            "links": links or {},
            "waiting_runs": _side_by_side(frozen["recommendations"], prod, source, v16, v17, wiki, links),
        }

        # Both waiting runs, their recommendations and proposal files, as frozen before the replay.
        now_recs = [dict(r) for r in prod.execute(
            "SELECT * FROM review_recommendations WHERE run_id IN (?, ?)", [r["run_id"] for r in frozen["runs"]])]
        now_runs = [dict(r) for r in prod.execute(
            "SELECT * FROM review_runs WHERE run_id IN (?, ?)", [r["run_id"] for r in frozen["runs"]])]
        changed_files = [k for k, v in frozen["proposal_hashes"].items()
                         if not (source / k).exists() or hashlib.sha256((source / k).read_bytes()).hexdigest() != v]
        key = lambda r: (r["run_id"], r["item_id"])
        result["waiting_runs_unchanged"] = {
            "runs_identical": sorted(now_runs, key=lambda r: r["run_id"]) == sorted(frozen["runs"], key=lambda r: r["run_id"]),
            "recommendations_identical": sorted(now_recs, key=key) == sorted(frozen["recommendations"], key=key),
            "proposal_files_checked": len(frozen["proposal_hashes"]),
            "proposal_files_changed": changed_files,
        }
        wiki_changed = [k for k, v in manifest["wiki_hashes"].items()
                        if not (wiki / k).exists() or hashlib.sha256((wiki / k).read_bytes()).hexdigest() != v]
        result["wiki_snapshot_unchanged"] = not wiki_changed
    (report_dir / "report.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    (report_dir / "report.md").write_text(_markdown(result))
    return result


def _markdown(r: dict) -> str:
    t16, t17 = r["threads"]["1.6"], r["threads"]["1.7"]
    lines = [
        "# B10 paired replay: extract@1.6 vs extract@1.7", "",
        f"Baseline `{r['baseline'][:7]}`, model `{r['model'].get('CMF_LOCAL_LLM_MODEL')}`, "
        f"{r['conversations']} conversations ({len(r['capture_only_conversations'])} capture-only), "
        f"{r['inference_windows_per_version']} inference windows per version.", "",
        "## Threads and merges", "",
        "| | 1.6 | 1.7 |", "|---|---|---|",
        f"| episodes written | {t16['episodes']} | {t17['episodes']} |",
        f"| review queue after merges | {t16['queue']} | {t17['queue']} |",
        f"| thread merges | {t16['merges']} | {t17['merges']} |",
        f"| distinct threads | {t16['threads']} | {t17['threads']} |",
        f"| threads spanning >1 project | {len(t16['cross_project_threads'])} | {len(t17['cross_project_threads'])} |",
        f"| absorbed children still queued | {t16['absorbed_children_still_queued']} | {t17['absorbed_children_still_queued']} |",
        "",
        f"1.7 rerun over its own journal (no model calls): {r['rerun_1_7']}", "",
    ]
    for v, t in (("1.6", t16), ("1.7", t17)):
        lines += [f"**Top-5 thread reuse, {v}:** " + "; ".join(
            f"`{x['thread']}` ({x['conversations']} conv, {x['episodes']} ep, {'/'.join(x['projects'])})" for x in t["top5_reuse"]), ""]
        if t["cross_project_threads"]:
            lines += [f"Cross-project threads, {v} (inspect):"] + [
                f"- `{x['thread']}`: {', '.join(x['projects'])}" for x in t["cross_project_threads"]] + [""]
    lines += ["## Document checks (1.7)", "", "| proposal | target | mode | dates | eval leak | live body kept |", "|---|---|---|---|---|---|"]
    for d in r["docs"]["1.7"]:
        dates = f"{d['created']} / {d['updated']} (source {d['source_days_chicago']})"
        lines.append(f"| `{d['proposal_id'][-8:]}` | `{d['target_path']}` | {d['mode']} | {dates} | "
                     f"{', '.join(d['eval_leaks']) or 'none'} | {d.get('live_body_preserved', 'new page')} |")
    lines += ["", "## Waiting runs, side by side (review aid, not verdicts)", ""]
    for g in r["waiting_runs"]:
        linked = f" (capture from transcript `{g['linked_transcript']}`)" if g.get("linked_transcript") else ""
        lines += [f"### `{g['conversation']}`" + (" (capture-only)" if g["capture_only"] else linked), ""]
        for it in g["items"]:
            p17 = it["replay_1_7"]
            lines.append(f"- **{it['label']}** ({it['run'][-6:]}, recommended {it['recommendation']}) → **{it['outcome']}**")
            lines.append(f"  - production: {it['production']['statement'][:400]}")
            if it.get("replay_1_6"):
                lines.append(f"  - 1.6 replay ({it['replay_1_6']['similarity']}): {it['replay_1_6']['statement'][:400]}")
            if p17:
                lines.append(f"  - 1.7 ({p17['similarity']}): {p17['statement'][:400]}")
        for a in g["added_in_1_7"]:
            lines.append(f"- **added in 1.7** (`{a['thread']}`): {a['statement'][:400]}")
        for d in g["docs"]:
            lines.append(f"- **{d['label']}** doc `{d['production']['target_path']}` → **{d['outcome']}**")
            for p in d["replay_1_7"]:
                lines.append(f"  - 1.7 `{p['target_path']}` ({p['mode']}, {p['operation']})")
                if p.get("addition"):
                    lines += ["", "    ```markdown"] + ["    " + l for l in p["addition"].splitlines()[:40]] + ["    ```", ""]
        lines.append("")
    u = r["waiting_runs_unchanged"]
    lines += ["## Guardrails", "",
              f"- Waiting runs identical: {u['runs_identical']}; recommendations identical: {u['recommendations_identical']}; "
              f"{u['proposal_files_checked']} proposal files checked, changed: {u['proposal_files_changed'] or 'none'}.",
              f"- Wiki snapshot unchanged by either replay: {r['wiki_snapshot_unchanged']}.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--link", action="append", default=[], metavar="CAPTURE_CONV=TRANSCRIPT_CONV",
                        help="compare a capture-only conversation against the transcript session that made it")
    args = parser.parse_args()
    links = dict(x.split("=", 1) for x in args.link)
    result = build(args.source.resolve(), args.out.resolve(), links)
    print(json.dumps({"threads": {v: {k: x for k, x in t.items() if k in ("episodes", "queue", "merges", "threads", "absorbed_children_still_queued")}
                                  for v, t in result["threads"].items()},
                      "cross_project": {v: len(t["cross_project_threads"]) for v, t in result["threads"].items()},
                      "rerun_1_7": result["rerun_1_7"], "waiting_runs_unchanged": result["waiting_runs_unchanged"],
                      "wiki_snapshot_unchanged": result["wiki_snapshot_unchanged"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
