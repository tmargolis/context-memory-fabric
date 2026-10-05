"""Point-in-time snapshots of the episodic graph and the wiki (MS8).

**Graph.** `snapshot_graph(as_of, target)` copies the source graph
(GRAPH.COPY) into a `replay-*` graph and rolls it back to `as_of`:

1. Every episode whose memory had not reached production yet is removed
   through Graphiti's own remove_episode, which also drops the facts those
   episodes created and entities only they mentioned. "Reached production"
   is the memory's first successful promotion into any graph of the
   production lineage (`PRODUCTION_LINEAGE`), keyed by memory_id. Each
   episode names its memory in its own source_description ("memory_id=...").
   Episode *names* are not stable: they are numbered at promotion time, and
   the 2026-09-13 rebuild reassigned 167 of them, while the ledger kept the
   names of the promotion it recorded. The source graph's own promoted_at is
   not enough either: the Spark migration re-promoted every memory on
   2026-09-09, so it dates a memory to its latest re-promotion. An episode
   with no memory_id (written straight through remember()) falls back to its
   own created_at.
2. Facts that a removed episode invalidated are made current again. Graphiti
   expires a fact while ingesting the episode that contradicts it and records
   no link between the two. Ingestion is sequential, though, so the
   invalidator is the episode whose ingestion was running at the fact's
   expired_at: the latest episode created at or before it.
3. With `wiki_paths` (the files in the wiki as exported at as_of), MS7b's
   wiki-derived Note nodes whose file did not exist yet are removed, with the
   entities only those notes mentioned. Notes carry no timestamp of their own.

The source graph is only read. Writes go to the snapshot, which must be named
`replay-*`, and production names are refused outright.

Remaining limit: Graphiti rewrites an entity's summary on every mention, so a
surviving entity's summary may reflect later episodes. Recall ranks facts and
episode text and never reads summaries, so grades are unaffected.

**Wiki.** `export_wiki_at(repo, as_of, dest)` extracts the LLM_Wiki git repo's
last commit before as_of into a scratch folder with `git archive` (nothing in
the repo is touched). The wiki is only as exact as its commit history:
`wiki_window(repo, as_of)` reports the commits either side of as_of and the
files changed between them, whose content at as_of is uncertain.
"""

from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import io
import re
import sqlite3
import subprocess
import tarfile
from pathlib import Path
from typing import Any, Iterable, Optional

SNAPSHOT_PREFIX = "replay-"
PROTECTED_GRAPHS = frozenset({"mem-fabric-local", "mem-fabric-gemini", "cmf_test"})
# Graphs that served production at some point, or rebuilt it. A memory is
# retrievable from its first successful promotion into any of them.
# Experiment graphs (mem-fabric-local-glm, ms4e-*) are deliberately absent:
# a memory promoted only there was never retrievable.
PRODUCTION_LINEAGE = (
    "mem-fabric-gemini",
    "mem-fabric-local",
    "mem-fabric-local-restore-20260912",
    "mem-fabric-local-wiki",
)


class ReplaySafetyError(RuntimeError):
    """A replay operation would write somewhere it must not."""


def check_snapshot_name(target: str, source: str) -> None:
    if target == source or target in PROTECTED_GRAPHS or not target.startswith(SNAPSHOT_PREFIX):
        raise ReplaySafetyError(
            f"refusing to write snapshot graph {target!r}: snapshots must be named "
            f"'{SNAPSHOT_PREFIX}*' and differ from the source and from production graphs"
        )


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _ts(value: Optional[str]) -> Optional[datetime]:
    """Parse a ledger or FalkorDB timestamp; naive values are UTC."""
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_MEMORY_ID = re.compile(r"memory_id=(\S+)")


def memory_id_of(source_description: Optional[str]) -> Optional[str]:
    """The memory an episode was promoted from, as its source_description records it."""
    m = _MEMORY_ID.search(source_description or "")
    return m.group(1) if m else None


def first_promotion_by_memory(journal_db: Path, lineage: Iterable[str] = PRODUCTION_LINEAGE) -> dict[str, str]:
    """memory_id -> its first successful promotion into a production-lineage graph."""
    graphs = tuple(dict.fromkeys(lineage))
    conn = sqlite3.connect(f"file:{journal_db}?mode=ro", uri=True)
    try:
        return dict(conn.execute(
            "SELECT memory_id, min(promoted_at) FROM promotions WHERE status = 'succeeded' "
            f"AND graph_name IN ({','.join('?' * len(graphs))}) GROUP BY memory_id",
            graphs,
        ).fetchall())
    finally:
        conn.close()


def availability(rows: Iterable[dict[str, Any]], first_by_memory: dict[str, str]) -> dict[str, str]:
    """episode name -> ISO time it became retrievable. `rows` carry name,
    source_description and created_at (graph_episode_rows or a snapshot query)."""
    out: dict[str, str] = {}
    for r in rows:
        mid = memory_id_of(r.get("source_description"))
        at = first_by_memory.get(mid) if mid else None
        if at is None:
            ts = _ts(r.get("created_at"))
            at = ts.isoformat() if ts else None
        if at is not None:
            out[r["name"]] = at
    return out


def graph_episode_rows(graph: str, redis_client: Any = None) -> list[dict[str, Any]]:
    """name, source_description and created_at of every episode in `graph` (read-only)."""
    if redis_client is None:
        import redis
        redis_client = redis.Redis()
    res = redis_client.execute_command(
        "GRAPH.RO_QUERY", graph,
        "MATCH (e:Episodic) RETURN e.name, e.source_description, toString(e.created_at)",
    )
    dec = lambda v: v.decode() if isinstance(v, bytes) else v  # noqa: E731
    return [{"name": dec(n), "source_description": dec(sd), "created_at": dec(ca)} for n, sd, ca in res[1]]


def memory_names(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    """memory_id -> the episode name it has in the graph `rows` came from."""
    return {mid: r["name"] for r in rows if (mid := memory_id_of(r.get("source_description")))}


@dataclass
class SnapshotResult:
    source: str
    target: str
    as_of: str
    episodes_before: int = 0
    episodes_removed: int = 0
    episodes_after: int = 0
    entities_after: int = 0
    invalidations_restored: int = 0
    invalidated_facts: int = 0
    notes_removed: int = 0
    note_entities_removed: int = 0
    removed_episode_names: list[str] = field(default_factory=list)


def _records(rows: Any) -> list[Any]:
    return rows[0] if rows and isinstance(rows[0], list) else (rows or [])


def _count(rows: Any) -> int:
    recs = _records(rows)
    return recs[0]["c"] if recs else 0


async def _restore_later_invalidations(
    driver: Any, timeline: list[tuple[datetime, str]], removed: set[str]
) -> int:
    """Clear expired_at/invalid_at on facts a removed episode invalidated."""
    if not removed or not timeline:
        return 0
    starts = [t for t, _ in timeline]
    expired = _records(await driver.execute_query(
        "MATCH ()-[r:RELATES_TO]->() WHERE r.expired_at IS NOT NULL "
        "RETURN r.uuid AS uuid, toString(r.expired_at) AS expired_at"
    ))
    restore = []
    for e in expired:
        at = _ts(e["expired_at"])
        i = bisect.bisect_right(starts, at) - 1 if at else -1
        if i >= 0 and timeline[i][1] in removed:
            restore.append(e["uuid"])
    if not restore:
        return 0
    return _count(await driver.execute_query(
        "MATCH ()-[r:RELATES_TO]->() WHERE r.uuid IN $uuids "
        "SET r.expired_at = NULL, r.invalid_at = NULL RETURN count(r) AS c",
        uuids=restore,
    ))


async def _prune_notes(driver: Any, wiki_paths: set[str]) -> tuple[int, int]:
    """Remove Note nodes whose file is not in the exported wiki, then the
    entities only those notes mentioned. Returns (notes, entities) removed."""
    notes = _records(await driver.execute_query("MATCH (n:Note) RETURN n.note_path AS path"))
    stale = sorted({n["path"] for n in notes if n["path"] and n["path"] not in wiki_paths})
    if not stale:
        return 0, 0
    mentioned = [r["uuid"] for r in _records(await driver.execute_query(
        "MATCH (n:Note)-[:MENTIONS]->(x:Entity) WHERE n.note_path IN $paths RETURN DISTINCT x.uuid AS uuid",
        paths=stale,
    ))]
    notes_removed = _count(await driver.execute_query(
        "MATCH (n:Note) WHERE n.note_path IN $paths "
        "WITH collect(n) AS ns, count(n) AS c FOREACH (x IN ns | DETACH DELETE x) RETURN c",
        paths=stale,
    ))
    entities_removed = 0
    if mentioned:
        entities_removed = _count(await driver.execute_query(
            "MATCH (x:Entity) WHERE x.uuid IN $uuids "
            "OPTIONAL MATCH (x)<-[m:MENTIONS]-() OPTIONAL MATCH (x)-[r:RELATES_TO]-() "
            "WITH x, count(m) AS ms, count(r) AS rs WHERE ms = 0 AND rs = 0 "
            "WITH collect(x) AS xs, count(x) AS c FOREACH (y IN xs | DETACH DELETE y) RETURN c",
            uuids=mentioned,
        ))
    return notes_removed, entities_removed


async def snapshot_graph(
    as_of: datetime,
    target: str,
    source: str = "mem-fabric-local",
    journal_db: Optional[Path] = None,
    redis_client: Any = None,
    graphiti: Any = None,
    wiki_paths: Optional[set[str]] = None,
    lineage: Iterable[str] = PRODUCTION_LINEAGE,
) -> SnapshotResult:
    """Build `target` as `source` looked at `as_of`. Idempotent: an existing
    target is dropped and rebuilt (it is, by construction, a replay-* scratch graph)."""
    check_snapshot_name(target, source)
    if journal_db is None:
        from server.journal.store import DEFAULT_JOURNAL_PATH
        journal_db = DEFAULT_JOURNAL_PATH
    if redis_client is None:
        import redis
        redis_client = redis.Redis()
    if redis_client.exists(target):
        redis_client.execute_command("GRAPH.DELETE", target)
    redis_client.execute_command("GRAPH.COPY", source, target)

    if graphiti is None:
        from server.providers.memory_graphiti import get_graphiti
        graphiti = get_graphiti(graph_name=target)
    driver = graphiti.driver
    cutoff = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)

    rows = _records(await driver.execute_query(
        "MATCH (e:Episodic) RETURN e.uuid AS uuid, e.name AS name, "
        "e.source_description AS source_description, toString(e.created_at) AS created_at"
    ))
    available = availability(rows, first_promotion_by_memory(journal_db, lineage))
    result = SnapshotResult(source=source, target=target, as_of=_iso(as_of), episodes_before=len(rows))
    timeline = sorted((t, r["uuid"]) for r in rows if (t := _ts(r["created_at"])) is not None)
    removed: set[str] = set()
    for r in rows:
        since = _ts(available.get(r["name"]))
        if since is None or since > cutoff:
            await graphiti.remove_episode(r["uuid"])
            removed.add(r["uuid"])
            result.removed_episode_names.append(r["name"])
    result.episodes_removed = len(removed)
    result.invalidations_restored = await _restore_later_invalidations(driver, timeline, removed)
    if wiki_paths is not None:
        result.notes_removed, result.note_entities_removed = await _prune_notes(driver, wiki_paths)

    counts = _records(await driver.execute_query(
        "MATCH (e:Episodic) WITH count(e) AS eps MATCH (n:Entity) RETURN eps, count(n) AS ents"
    ))
    if counts:
        result.episodes_after, result.entities_after = counts[0]["eps"], counts[0]["ents"]
    result.invalidated_facts = _count(await driver.execute_query(
        "MATCH ()-[r:RELATES_TO]->() WHERE r.invalid_at IS NOT NULL RETURN count(r) AS c"
    ))
    return result


def drop_snapshot(target: str, redis_client: Any = None) -> bool:
    check_snapshot_name(target, source="")
    if redis_client is None:
        import redis
        redis_client = redis.Redis()
    if not redis_client.exists(target):
        return False
    redis_client.execute_command("GRAPH.DELETE", target)
    return True


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def wiki_commit_at(repo: Path, as_of: datetime) -> Optional[str]:
    return _git(repo, "rev-list", "-1", f"--before={as_of.isoformat()}", "HEAD") or None


# LLM_Wiki's watcher (~/bin/obsidian-git-sync.sh) commits "Auto-sync <time>"
# about 20 s after a change, later if a push is still running.
AUTO_COMMIT_PREFIX = "Auto-sync"
AUTO_COMMIT_LATENCY_S = 300


@dataclass
class WikiWindow:
    """The commits either side of a cut-off. The files changed between them
    changed at some moment in that window, so their content at the cut-off is
    uncertain, unless the next commit is an auto-commit made well after the
    cut-off: a running watcher commits within seconds of a change, so those
    changes happened after the cut-off. Caveat: git alone cannot tell a
    watcher's catch-up commit after an outage from a prompt one."""

    commit: Optional[str] = None
    commit_time: Optional[str] = None
    next_commit: Optional[str] = None
    next_time: Optional[str] = None
    next_is_auto: bool = False
    uncertain_paths: list[str] = field(default_factory=list)

    @property
    def gap_hours(self) -> Optional[float]:
        start, end = _ts(self.commit_time), _ts(self.next_time)
        return round((end - start).total_seconds() / 3600, 2) if start and end else None

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "gap_hours": self.gap_hours, "uncertain_paths": len(self.uncertain_paths)}


def wiki_window(
    repo: Path,
    as_of: datetime,
    auto_prefix: str = AUTO_COMMIT_PREFIX,
    auto_latency_s: int = AUTO_COMMIT_LATENCY_S,
) -> WikiWindow:
    w = WikiWindow(commit=wiki_commit_at(repo, as_of))
    if w.commit:
        w.commit_time = _git(repo, "show", "-s", "--format=%cI", w.commit)
    later = _git(repo, "rev-list", "--reverse", f"--after={as_of.isoformat()}", "HEAD").splitlines()
    if later:
        w.next_commit = later[0]
        w.next_time = _git(repo, "show", "-s", "--format=%cI", w.next_commit)
        subject = _git(repo, "show", "-s", "--format=%s", w.next_commit)
        w.next_is_auto = bool(auto_prefix) and subject.startswith(auto_prefix)
        next_at, cutoff = _ts(w.next_time), (as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc))
        prompt_after = w.next_is_auto and next_at is not None and (next_at - cutoff).total_seconds() > auto_latency_s
        if w.commit and not prompt_after:
            w.uncertain_paths = _git(repo, "diff", "--name-only", w.commit, w.next_commit).splitlines()
    return w


def export_wiki_at(repo: Path, as_of: datetime, dest: Path) -> Optional[str]:
    """Extract the wiki as of `as_of` into `dest`. Returns the commit used, or
    None if the repo has no commit that old (dest is then left empty)."""
    commit = wiki_commit_at(repo, as_of)
    dest.mkdir(parents=True, exist_ok=True)
    if commit is None:
        return None
    blob = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit],
                          capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        tar.extractall(dest, filter="data")
    return commit


def exported_paths(root: Path) -> set[str]:
    """Wiki-relative POSIX paths of every file under an export."""
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
