"""Time-boxed, self-tuning Cowork extraction driver (2026-10-02).

Runs `claude_cowork` ExtractPolicy consolidation on the Spark LLM in
event-budgeted batches, highest predicted yield first, for about
--run-minutes, then exits so the operator can report and relaunch. It never
sends more than one conversation to Spark at a time (same as the worker),
and tunes itself from what it measures:

  - batch size: an events budget = --batch-minutes / (EMA seconds per event),
    packed from the top of `pending_extract_ranked` (assistant prose per
    event, see worker.py). A conversation bigger than the budget runs alone;
    at most --max-batch-conversations per batch;
  - slowdown: a batch whose seconds-per-event exceeds 1.6x the running
    average halves the next budget and lengthens the cool-down to 5 minutes
    (Spark is being shared, thermally limited, or swapping);
  - health: before every batch the tunnel's /v1/models must answer within
    10 s; otherwise back off and retry, and stop (exit 3) after 5 tries;
  - errors: a batch with any consolidation error counts; 3 in a row stops
    (exit 4);
  - the shared Spark slot: if another Spark job (a poller, Phase 4) holds
    imports/journal/spark_job.lock, wait and retry rather than fight it;
  - one driver at a time (imports/journal/cowork_driver.lock);
  - continuous: batches follow each other until --run-minutes (default 95,
    so the last batch, normally ~15 min and at most ~25, still ends inside the
    2-hour limit on a background command); the operator relaunches at once.
    Batches are never shrunk or skipped to fit the window;
  - live control: imports/review/cowork_driver_control.json is re-read before
    every batch and while paused -- {"pause": bool, "stop": bool,
    "batch_minutes": n, "cooldown_sec": n, "max_batch_conversations": n};
  - live status: imports/review/cowork_driver_status.json;
  - interruption-safe: the conversations of the running batch are noted in
    imports/review/cowork_driver_inflight.json; a conversation killed
    half-way already has job rows (so it no longer counts as pending), and
    the next run finishes it first. Prefer {"stop": true} to killing.

Per-batch metrics append to imports/review/cowork_extract_log.csv, and
per-conversation results (title, events, episodes, likely keepers) to
imports/review/cowork_extract_conversations.jsonl -- both gitignored.
"Likely keeper" = decision / plan / rejected_alternative with >= 3 evidence
turns, the rule from the user's review history.

    uv run python scripts/cowork_extract_driver.py --stop-at 2026-10-03T13:00:00+00:00
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import urllib.request

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env")

from server.adapters.claude_cowork.worker import (  # noqa: E402
    PendingConversation,
    extract_pending,
    pending_extract_ranked,
    THIN_ASSISTANT_CHARS,
)
from server.consolidation.store import ConsolidationStore  # noqa: E402
from server.journal.store import DEFAULT_JOURNAL_PATH, SqliteEventStore  # noqa: E402

LOG_DIR = _ROOT / "imports" / "review"
LOG_CSV = LOG_DIR / "cowork_extract_log.csv"
LOG_CONV = LOG_DIR / "cowork_extract_conversations.jsonl"
CONTROL = LOG_DIR / "cowork_driver_control.json"
STATUS = LOG_DIR / "cowork_driver_status.json"
INFLIGHT = LOG_DIR / "cowork_driver_inflight.json"
DRIVER_LOCK = DEFAULT_JOURNAL_PATH.parent / "cowork_driver.lock"
INITIAL_SEC_PER_EVENT = 3.1  # measured over the first 30 conversations
KEEPER_SQL = ("d.reasoning_kind IN ('decision','plan','rejected_alternative') "
              "AND json_array_length(d.evidence_event_ids_json) >= 3")


def now() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(f"[{now():%H:%M:%S}] {msg}", flush=True)


def spark_healthy(timeout: float = 10.0) -> tuple[bool, float]:
    url = os.getenv("CMF_LOCAL_BASE_URL", "http://127.0.0.1:12345/v1").rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {os.getenv('CMF_LOCAL_API_KEY', '')}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ok = r.status == 200 and os.getenv("CMF_LOCAL_LLM_MODEL", "") in r.read().decode()
    except Exception:  # noqa: BLE001 -- any failure is "unhealthy"
        return False, time.time() - t0
    return ok, time.time() - t0


def pack_batch(ranked: list[PendingConversation], events_budget: int, max_convs: int) -> list[PendingConversation]:
    batch, total = [], 0
    for r in ranked:
        if batch and (total + r.events > events_budget or len(batch) >= max_convs):
            break
        batch.append(r)
        total += r.events
        if total >= events_budget:
            break
    return batch


def results_for(conv_ids: list[str]) -> dict[str, dict]:
    marks = ",".join("?" * len(conv_ids))
    with sqlite3.connect(f"file:{DEFAULT_JOURNAL_PATH}?mode=ro", uri=True) as c:
        titles = dict(c.execute(
            f"SELECT conversation_id, max(json_extract(metadata_json,'$.conversation_title')) FROM events "
            f"WHERE harness='claude_cowork' AND conversation_id IN ({marks}) GROUP BY 1", conv_ids))
        rows = c.execute(
            f"SELECT x.conversation_id, count(*), sum({KEEPER_SQL}) FROM derived_memories d "
            f"JOIN events x ON x.event_id = d.source_event_id WHERE x.conversation_id IN ({marks}) GROUP BY 1", conv_ids).fetchall()
    out = {c: {"title": titles.get(c), "episodes": 0, "keepers": 0} for c in conv_ids}
    for conv, eps, keep in rows:
        out[conv].update(episodes=eps, keepers=keep or 0)
    return out


def doc_proposal_count() -> int:
    n = 0
    for p in (_ROOT / "doc-proposals").glob("*.json"):
        try:
            n += json.loads(p.read_text()).get("source_harness") == "claude_cowork"
        except (OSError, json.JSONDecodeError):
            pass
    return n


def read_control(started_wall: float = 0.0) -> dict:
    """The control file's contents. A {"stop": true} written *before* this
    driver started is stale (it ended the previous run) and is ignored, so a
    relaunch never has to clear the file."""
    try:
        data = json.loads(CONTROL.read_text())
        if not isinstance(data, dict):
            return {}
        if data.get("stop") and CONTROL.stat().st_mtime < started_wall:
            data = {k: v for k, v in data.items() if k != "stop"}
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def write_status(**fields) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fields["updated"] = now().isoformat(timespec="seconds")
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(fields, indent=1, default=str))
    tmp.replace(STATUS)


def run_extract(ids: list[str], *, force: bool = False, status: dict | None = None):
    """extract_pending for `ids`, waiting out other Spark jobs (a poller pass
    holds the shared slot for minutes at a time). Returns (stats, seconds).
    `status` is republished as state="extracting" before every attempt, so the
    status file never keeps saying "waiting" once the slot has been won."""
    t0 = time.time()
    stats = None
    for wait in range(15):
        if status is not None:
            write_status(state="extracting", waited_for_slot_s=round(time.time() - t0), **status)
        with SqliteEventStore(None) as store:
            stats = extract_pending(store, ConsolidationStore(None), conversations=ids, force=force)
        if not stats.skipped_reason:
            break
        log(f"Spark slot busy ({stats.skipped_reason}); waiting 60 s ({wait + 1}/15)")
        write_status(state="waiting_for_spark_slot", reason=stats.skipped_reason, **(status or {}))
        time.sleep(60)
    return stats, time.time() - t0


def seed_from_log(n_batches: int = 6) -> tuple[float, int | None]:
    """(events-weighted seconds/event, mean events per batch) from the most
    recent batches of earlier runs, so a relaunch does not restart from the
    conservative default, and one tiny, flattering batch cannot size the next
    one at 10x. (INITIAL_SEC_PER_EVENT, None) when there is no history."""
    try:
        rows = [r for r in csv.DictReader(LOG_CSV.open()) if r.get("events") and r.get("seconds")]
    except OSError:
        return INITIAL_SEC_PER_EVENT, None
    rows = rows[-n_batches:]
    events = sum(int(r["events"]) for r in rows)
    if not events:
        return INITIAL_SEC_PER_EVENT, None
    return sum(float(r["seconds"]) for r in rows) / events, events // len(rows)


MAX_BATCH_GROWTH = 1.6  # a batch may be at most this many times the previous batch's events


def append_csv(row: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    new = not LOG_CSV.exists()
    with LOG_CSV.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stop-at", required=True, help="ISO8601 overall deadline; no batch starts after it")
    ap.add_argument("--run-minutes", type=float, default=95, help="no new batch starts after this many minutes (keeps the run under a 2-hour background limit)")
    ap.add_argument("--batch-minutes", type=float, default=15, help="target Spark minutes per batch")
    ap.add_argument("--max-batch-conversations", type=int, default=25)
    ap.add_argument("--cooldown-sec", type=float, default=30, help="pause between batches (a slowdown raises it to 5 min)")
    ap.add_argument("--dry-run", action="store_true", help="show the next batch and exit")
    args = ap.parse_args()
    stop_at = datetime.fromisoformat(args.stop_at)

    lock_fd = os.open(DRIVER_LOCK, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another cowork_extract_driver is running; exiting")
        return 2

    started = time.time()
    started_wall = started
    sec_per_event, prev_events = seed_from_log()
    ema_seen = True  # seeded from earlier runs; every measurement is smoothed into it
    shrink_next = False
    consecutive_error_batches = 0
    totals = dict(batches=0, conversations=0, events=0, seconds=0.0, episodes=0, keepers=0, docs=0, errors=0)
    reason = "time box reached"

    # An earlier run killed mid-batch left conversations half-extracted.
    if INFLIGHT.exists() and not args.dry_run:
        try:
            stale = json.loads(INFLIGHT.read_text()).get("ids") or []
        except (OSError, json.JSONDecodeError):
            stale = []
        if stale:
            log(f"finishing {len(stale)} conversation(s) interrupted by a previous run")
            write_status(state="recovering_interrupted_batch", conversations=len(stale))
            stats, secs = run_extract(stale, force=True)
            if stats is None or stats.skipped_reason:
                return finish(totals, started, "Spark slot never freed during recovery", 5)
            log(f"  recovered in {secs / 60:.1f} min, {len(stats.errors)} errors")
            INFLIGHT.unlink(missing_ok=True)

    while True:
        ctl = read_control(started_wall)
        elapsed_min = (time.time() - started) / 60
        if ctl.get("stop"):
            reason = "stop requested via control file"
            break
        if now() >= stop_at:
            reason = "overall deadline reached"
            break
        if elapsed_min >= args.run_minutes:
            break
        if ctl.get("pause"):
            write_status(state="paused", elapsed_min=round(elapsed_min, 1))
            time.sleep(30)
            continue

        batch_minutes = float(ctl.get("batch_minutes", args.batch_minutes))
        cooldown_base = float(ctl.get("cooldown_sec", args.cooldown_sec))
        max_convs = int(ctl.get("max_batch_conversations", args.max_batch_conversations))

        with SqliteEventStore(None) as store:
            ranked = pending_extract_ranked(store)
        workable = [r for r in ranked if r.events >= 3]
        if not workable:
            reason = "nothing pending"
            break

        budget = max(int(batch_minutes * 60 / sec_per_event), 1)
        if prev_events:
            budget = min(budget, max(int(prev_events * MAX_BATCH_GROWTH), 100))
        if shrink_next:
            budget = max(budget // 2, 1)
            shrink_next = False
        batch = pack_batch(workable, budget, max_convs)
        n_events = sum(r.events for r in batch)
        predicted_min = n_events * sec_per_event / 60
        ids = [r.conversation_id for r in batch]
        log(f"batch {totals['batches'] + 1}: {len(batch)} conversations, {n_events} events "
            f"(budget {budget}, ~{predicted_min:.0f} min at {sec_per_event:.2f} s/event); "
            f"prose/event {batch[0].prose_per_event:.0f}..{batch[-1].prose_per_event:.0f}; {len(workable) - len(batch)} pending after")
        if args.dry_run:
            return 0

        for attempt in range(1, 6):
            write_status(state="probing_spark", attempt=attempt)
            ok, latency = spark_healthy()
            if ok:
                break
            log(f"Spark not healthy (probe {latency:.1f}s, try {attempt}/5); backing off 120 s")
            time.sleep(120)
        else:
            reason = "Spark unhealthy after 5 probes"
            totals["errors"] += 1
            append_csv({"ts": now().isoformat(timespec="seconds"), "note": reason})
            log(reason)
            return finish(totals, started, reason, 3)

        docs_before = doc_proposal_count()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        INFLIGHT.write_text(json.dumps({"ids": ids, "started": now().isoformat(timespec="seconds")}))
        status_fields = dict(batch=totals["batches"] + 1, conversations=len(ids), events=n_events,
                             predicted_minutes=round(predicted_min, 1), sec_per_event=round(sec_per_event, 2),
                             batch_started=now().isoformat(timespec="seconds"), run_elapsed_min=round(elapsed_min, 1),
                             totals=dict(totals))
        stats, seconds = run_extract(ids, status=status_fields)
        if stats is None or stats.skipped_reason:
            reason = f"Spark slot never freed ({stats.skipped_reason if stats else '?'})"
            log(reason)
            return finish(totals, started, reason, 5)
        INFLIGHT.unlink(missing_ok=True)

        res = results_for(ids)
        episodes = sum(v["episodes"] for v in res.values())
        keepers = sum(v["keepers"] for v in res.values())
        docs = doc_proposal_count() - docs_before
        measured = seconds / max(n_events, 1)
        cooldown = cooldown_base
        note = ""
        if ema_seen and measured > 1.6 * sec_per_event:
            shrink_next, cooldown = True, max(cooldown_base, 300)
            note = f"slowdown {measured:.2f} vs {sec_per_event:.2f} s/event: halving next batch, longer cool-down"
            log(note)
        sec_per_event = 0.6 * sec_per_event + 0.4 * measured
        prev_events = n_events

        totals["batches"] += 1
        totals["conversations"] += len(ids)
        totals["events"] += n_events
        totals["seconds"] += seconds
        totals["episodes"] += episodes
        totals["keepers"] += keepers
        totals["docs"] += docs
        totals["errors"] += len(stats.errors)
        append_csv({"ts": now().isoformat(timespec="seconds"), "batch": totals["batches"], "conversations": len(ids),
                    "events": n_events, "seconds": round(seconds), "sec_per_event": round(measured, 2),
                    "episodes": episodes, "likely_keepers": keepers, "doc_proposals": docs,
                    "errors": len(stats.errors), "cooldown_s": cooldown, "note": note})
        with LOG_CONV.open("a") as f:
            for r in batch:
                f.write(json.dumps({"ts": now().isoformat(timespec="seconds"), "conversation_id": r.conversation_id,
                                    "events": r.events, "prose_per_event": round(r.prose_per_event),
                                    **res[r.conversation_id]}) + "\n")
        log(f"  done in {seconds / 60:.1f} min ({measured:.2f} s/event): {episodes} episodes, {keepers} likely keepers, "
            f"{docs} doc proposals, {len(stats.errors)} errors")

        if stats.errors:
            consecutive_error_batches += 1
            for e in stats.errors[:3]:
                log(f"  error: {e[:200]}")
            if consecutive_error_batches >= 3:
                reason = "3 consecutive batches with errors"
                return finish(totals, started, reason, 4)
        else:
            consecutive_error_batches = 0
        write_status(state="cooling_down", cooldown_s=cooldown, sec_per_event=round(sec_per_event, 2), totals=totals,
                     run_elapsed_min=round((time.time() - started) / 60, 1))
        time.sleep(cooldown)

    return finish(totals, started, reason, 0)


def finish(totals: dict, started: float, reason: str, code: int) -> int:
    with SqliteEventStore(None) as store:
        ranked = pending_extract_ranked(store)
    workable = [r for r in ranked if r.events >= 3]
    remaining_events = sum(r.events for r in workable)
    good = [r for r in workable if r.assistant_chars >= THIN_ASSISTANT_CHARS and r.typed_turns >= 2 and r.real_replies >= 2]
    summary = {
        "exit_reason": reason,
        "this_run": {**totals, "minutes": round((time.time() - started) / 60, 1), "spark_minutes": round(totals["seconds"] / 60, 1)},
        "pending_conversations": len(workable),
        "pending_events": remaining_events,
        "pending_substantive": len(good),
        "pending_substantive_events": sum(r.events for r in good),
    }
    write_status(state="exited", **summary)
    print("SUMMARY " + json.dumps(summary), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
