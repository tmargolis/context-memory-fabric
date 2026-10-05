"""Relabel Claude transcript provenance in the journal and review stores (2026-10-02).

docs/CLAUDE-HARNESS-PROVENANCE-PLAN.md step 4. Until 2026-10-02 the MS4b
parser stamped every ~/.claude/projects transcript `harness="claude_code"`,
and the temp-dir answer-eval `claude -p` runs were journaled with them.
This script, driven by each event's own `metadata.entrypoint` and
`metadata.project_path`:

  1. deletes `claude_code` events from temp-dir (ephemeral) projects, plus
     their `claude_code_tail_state` rows -- aborting if any derived memory,
     consolidation job or thread references one;
  2. relabels `claude_code` events by entrypoint: claude-desktop ->
     claude_desktop_code, local-agent -> claude_cowork (cli / sdk-* / none
     stay claude_code -- the 19 MCP-boundary rows have no entrypoint and
     can't be split, see server/capture/identity.py);
  3. relabels the MCP-boundary fallback slug
     local_agent_mode_context_memory_fabric -> claude_cowork;
  4. rewrites `reasoning_threads.harnesses_json`, the episode mirror files
     (episode-proposals/**, `"harness"`) and doc proposals (doc-proposals/**,
     `"source_harness"`) for the relabeled conversations, so
     list_review_conversations(harness=...) agrees with the journal.

event_id and memory_id strings are identifiers and are never touched.

Dry run by default (read-only). `--apply` first backs up journal.db
(sqlite backup API) and tars both mirror dirs into imports/journal/bak/,
then makes every DB change in one transaction.

    uv run python scripts/migrate_claude_harness_provenance.py
    uv run python scripts/migrate_claude_harness_provenance.py --apply
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import tarfile

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from server.adapters.claude_code.parser import harness_for_entrypoint  # noqa: E402
from server.adapters.claude_code.transcript_reader import is_ephemeral_project  # noqa: E402
from server.journal.store import DEFAULT_JOURNAL_PATH  # noqa: E402

OLD = "claude_code"
COWORK_FALLBACK_SLUG = "local_agent_mode_context_memory_fabric"
COWORK = "claude_cowork"
EPISODE_DIR = _ROOT / "episode-proposals"
DOC_DIR = _ROOT / "doc-proposals"
BAK_DIR = _ROOT / "imports" / "journal" / "bak"
STAMP = "20261002-pre-provenance"


def plan(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT event_id, conversation_id, event_type, "
        "json_extract(metadata_json,'$.entrypoint') AS ep, "
        "json_extract(metadata_json,'$.project_path') AS pp "
        "FROM events WHERE harness = ?",
        (OLD,),
    ).fetchall()

    delete_ids: list[str] = []
    relabel: dict[str, list[str]] = defaultdict(list)  # new harness -> event_ids
    conv_new: dict[str, set[str]] = defaultdict(set)  # conversation -> harnesses after migration
    deleted_convs: set[str] = set()
    for event_id, conv, _etype, ep, pp in rows:
        if pp and is_ephemeral_project(pp):
            delete_ids.append(event_id)
            deleted_convs.add(conv)
            continue
        new = harness_for_entrypoint(ep, default=OLD)
        conv_new[conv].add(new)
        if new != OLD:
            relabel[new].append(event_id)

    mixed = {c: sorted(h) for c, h in conv_new.items() if len(h) > 1}
    # Only conversations that move wholesale get their mirrors relabeled.
    conv_map = {c: next(iter(h)) for c, h in conv_new.items() if len(h) == 1 and next(iter(h)) != OLD}

    cowork_slug_ids = [r[0] for r in conn.execute("SELECT event_id FROM events WHERE harness = ?", (COWORK_FALLBACK_SLUG,))]

    # Safety: nothing derived may point at an event we delete.
    refs = Counter()
    job_ids: list[str] = []
    if delete_ids:
        conn.execute("CREATE TEMP TABLE _del(event_id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO _del VALUES (?)", [(i,) for i in delete_ids])
        refs["derived_memories.source_event_id"] = conn.execute(
            "SELECT count(*) FROM derived_memories WHERE source_event_id IN (SELECT event_id FROM _del)").fetchone()[0]
        # Jobs that produced nothing (all triaged_out "below reasoning floor"
        # for the 1-2 event eval runs) are deleted with their events; one
        # that produced a memory blocks the migration.
        refs["consolidation_jobs.with_derived_memory"] = conn.execute(
            "SELECT count(*) FROM consolidation_jobs WHERE source_event_id IN (SELECT event_id FROM _del) "
            "AND derived_memory_id IS NOT NULL").fetchone()[0]
        job_ids = [r[0] for r in conn.execute(
            "SELECT job_id FROM consolidation_jobs WHERE source_event_id IN (SELECT event_id FROM _del)")]
        refs["derived_memories.evidence"] = conn.execute(
            "SELECT count(*) FROM derived_memories d, json_each(d.evidence_event_ids_json) j "
            "WHERE j.value IN (SELECT event_id FROM _del)").fetchone()[0]
        refs["reasoning_threads.conversation"] = sum(
            1 for (cj,) in conn.execute("SELECT conversation_ids_json FROM reasoning_threads")
            if set(json.loads(cj or "[]")) & deleted_convs)
        conn.execute("DROP TABLE _del")

    tail_rows = [
        fp for fp, slug in conn.execute("SELECT file_path, project_slug FROM claude_code_tail_state")
        if slug and is_ephemeral_project(slug)]

    threads = []
    for key, hj, cj in conn.execute("SELECT thread_key, harnesses_json, conversation_ids_json FROM reasoning_threads"):
        hs = json.loads(hj or "[]")
        if OLD not in hs:
            continue
        convs = json.loads(cj or "[]")
        replacement = {conv_map[c] for c in convs if c in conv_map}
        still_old = any(c in conv_new and OLD in conv_new[c] for c in convs)
        if not replacement:
            continue
        new_hs = [h for h in hs if h != OLD] + ([OLD] if still_old else [])
        new_hs += sorted(replacement - set(new_hs))
        if new_hs != hs:
            threads.append((key, hs, new_hs))

    def scan(base: Path, field: str, conv_field: str):
        hits, unmapped = [], Counter()
        if not base.is_dir():
            return hits, unmapped
        needle = f'"{field}": "{OLD}"'
        for p in base.rglob("*.json"):
            text = p.read_text(encoding="utf-8")
            if needle not in text:
                continue
            try:
                conv = json.loads(text).get(conv_field)
            except json.JSONDecodeError:
                unmapped["unparseable"] += 1
                continue
            if conv in conv_map:
                hits.append((p, conv_map[conv]))
            else:
                unmapped["cli_or_unmapped" if conv in conv_new else "conversation_not_in_journal"] += 1
        return hits, unmapped

    ep_hits, ep_unmapped = scan(EPISODE_DIR, "harness", "conversation_id")
    doc_hits, doc_unmapped = scan(DOC_DIR, "source_harness", "source_conversation_id")

    return {
        "delete_ids": delete_ids, "deleted_convs": deleted_convs, "relabel": relabel,
        "conv_map": conv_map, "mixed": mixed, "cowork_slug_ids": cowork_slug_ids, "refs": refs,
        "tail_rows": tail_rows, "job_ids": job_ids, "threads": threads,
        "ep_hits": ep_hits, "ep_unmapped": ep_unmapped, "doc_hits": doc_hits, "doc_unmapped": doc_unmapped,
    }


def harness_counts(conn: sqlite3.Connection) -> dict:
    return {h: (n, c) for h, n, c in conn.execute(
        "SELECT harness, count(*), count(DISTINCT conversation_id) FROM events "
        "WHERE harness LIKE 'claude%' OR harness = ? GROUP BY harness ORDER BY harness", (COWORK_FALLBACK_SLUG,))}


def report(p: dict, before: dict) -> None:
    print("== events by harness (events, conversations), BEFORE")
    for h, (n, c) in before.items():
        print(f"   {h:42s} {n:7d} {c:6d}")
    print(f"\n== delete: {len(p['delete_ids'])} ephemeral-project events in {len(p['deleted_convs'])} conversations")
    print(f"   + {len(p['tail_rows'])} claude_code_tail_state rows, {len(p['job_ids'])} memory-less consolidation_jobs rows")
    print(f"   references from derived data (must all be 0): {dict(p['refs'])}")
    for h, ids in sorted(p["relabel"].items()):
        convs = sum(1 for v in p["conv_map"].values() if v == h)
        print(f"== relabel claude_code -> {h}: {len(ids)} events, {convs} conversations")
    print(f"== relabel {COWORK_FALLBACK_SLUG} -> {COWORK}: {len(p['cowork_slug_ids'])} events")
    print(f"== mixed-entrypoint conversations (events relabeled, mirrors left alone): {len(p['mixed'])} {p['mixed'] or ''}")
    print(f"== reasoning_threads harnesses_json updates: {len(p['threads'])}")
    for key, old, new in p["threads"][:5]:
        print(f"   {key}: {old} -> {new}")
    print(f"== episode mirrors to relabel: {len(p['ep_hits'])}  (left as claude_code: {dict(p['ep_unmapped'])})")
    print(f"== doc proposals to relabel:   {len(p['doc_hits'])}  (left as claude_code: {dict(p['doc_unmapped'])})")


def apply(db_path: Path, conn: sqlite3.Connection, p: dict) -> None:
    if any(p["refs"].values()):
        raise SystemExit(f"ABORT: derived data references events marked for deletion: {dict(p['refs'])}")
    BAK_DIR.mkdir(parents=True, exist_ok=True)
    bak_db = BAK_DIR / f"journal-{STAMP}.db"
    if bak_db.exists():
        raise SystemExit(f"ABORT: backup {bak_db} already exists -- refusing to overwrite it")
    with sqlite3.connect(bak_db) as dst:
        conn.backup(dst)
    print(f"backed up journal -> {bak_db}")
    for d in (EPISODE_DIR, DOC_DIR):
        if d.is_dir():
            tar_path = BAK_DIR / f"{d.name}-{STAMP}.tar.gz"
            with tarfile.open(tar_path, "w:gz") as tar:
                tar.add(d, arcname=d.name)
            print(f"backed up {d.name}/ -> {tar_path}")

    now = datetime.now().isoformat()
    with conn:  # one transaction
        conn.executemany("DELETE FROM events WHERE event_id = ?", [(i,) for i in p["delete_ids"]])
        conn.executemany("DELETE FROM claude_code_tail_state WHERE file_path = ?", [(f,) for f in p["tail_rows"]])
        conn.executemany("DELETE FROM consolidation_jobs WHERE job_id = ? AND derived_memory_id IS NULL",
                         [(j,) for j in p["job_ids"]])
        for new, ids in p["relabel"].items():
            conn.executemany("UPDATE events SET harness = ? WHERE event_id = ? AND harness = ?",
                             [(new, i, OLD) for i in ids])
        conn.execute("UPDATE events SET harness = ? WHERE harness = ?", (COWORK, COWORK_FALLBACK_SLUG))
        for key, _old, new in p["threads"]:
            conn.execute("UPDATE reasoning_threads SET harnesses_json = ?, updated_at = ? WHERE thread_key = ?",
                         (json.dumps(new), now, key))
    print("journal transaction committed")

    for field, hits in (("harness", p["ep_hits"]), ("source_harness", p["doc_hits"])):
        for path, new in hits:
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace(f'"{field}": "{OLD}"', f'"{field}": "{new}"', 1), encoding="utf-8")
        print(f"rewrote {len(hits)} files ({field})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_JOURNAL_PATH)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    # Dry run opens the journal read-only; plan()'s TEMP table lives in the
    # separate temp database, which mode=ro still allows.
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) if not args.apply else sqlite3.connect(args.db)
    conn.execute("PRAGMA temp_store = MEMORY")
    before = harness_counts(conn)
    p = plan(conn)
    report(p, before)
    if not args.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply.")
        return 0
    apply(args.db, conn, p)
    print("\n== events by harness AFTER")
    for h, (n, c) in harness_counts(conn).items():
        print(f"   {h:42s} {n:7d} {c:6d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
