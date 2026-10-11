"""MS8 — replay harness: safety, snapshot selection, grading, policies, export.

Fakes only; the live replay run is recorded in the MS8 report, not here.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from server.replay import snapshot as snap
from server.replay.grading import grade_case, summarize
from server.replay.policies import EDGE_ONLY, EDGE_PLUS_EPISODE_VECTOR, applied
from server.replay.runner import Snapshot, run_replay, write_report

T = datetime(2026, 9, 10, tzinfo=timezone.utc)


def _journal(rows):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    conn = sqlite3.connect(tmp.name)
    conn.execute("CREATE TABLE promotions (memory_id TEXT, status TEXT, episode_name TEXT, graph_name TEXT, promoted_at TEXT)")
    conn.executemany("INSERT INTO promotions VALUES (?, 'succeeded', ?, ?, ?)", rows)
    conn.commit(); conn.close()
    return Path(tmp.name)


class TestSafety(unittest.TestCase):
    def test_refuses_production_and_unprefixed_targets(self):
        for target, source in [("mem-fabric-local", "mem-fabric-local"), ("replay-x", "replay-x"),
                               ("scratch", "mem-fabric-local"), ("mem-fabric-gemini", "mem-fabric-local")]:
            with self.subTest(target=target), self.assertRaises(snap.ReplaySafetyError):
                snap.check_snapshot_name(target, source)
        snap.check_snapshot_name("replay-20260910T0000", "mem-fabric-local")

    def test_drop_refuses_production(self):
        with self.assertRaises(snap.ReplaySafetyError):
            snap.drop_snapshot("mem-fabric-local", redis_client=object())


class FakeRedis:
    def __init__(self):
        self.commands, self.graphs = [], {"mem-fabric-local"}

    def exists(self, name):
        return name in self.graphs

    def execute_command(self, *args):
        self.commands.append(args)
        if args[0] == "GRAPH.COPY":
            self.graphs.add(args[2])
        if args[0] == "GRAPH.DELETE":
            self.graphs.discard(args[1])


class FakeDriver:
    def __init__(self, episodes):
        self.episodes = episodes
        self.queries = []

    async def execute_query(self, q, **p):
        self.queries.append(q)
        if "RETURN e.uuid AS uuid" in q:
            return [[dict(e) for e in self.episodes]]
        if "FOREACH" in q:
            return [[{"c": 2}]]
        if "RETURN eps" in q:
            return [[{"eps": 1, "ents": 5}]]
        if "invalid_at IS NOT NULL" in q:
            return [[{"c": 1}]]
        return [[]]


class FakeGraphiti:
    def __init__(self, episodes):
        self.driver = FakeDriver(episodes)
        self.removed = []

    async def remove_episode(self, uuid):
        self.removed.append(uuid)


class TestSnapshot(unittest.IsolatedAsyncioTestCase):
    async def test_removes_only_episodes_after_the_cutoff(self):
        journal = _journal([
            # The ledger's names are stale on purpose: a rebuild renumbered the episodes.
            ("m1", "old-009", "mem-fabric-local", "2026-09-09T16:30:00+00:00"),
            ("m2", "old-001", "mem-fabric-local", "2026-09-11T10:00:00+00:00"),
            ("m3", "other-graph", "ms4e-A-legacy", "2026-09-28T00:00:00+00:00"),
        ])
        episodes = [
            {"uuid": "u1", "name": "old-001", "source_description": "x | memory_id=m1",
             "created_at": "2026-09-13T00:00:00+00:00"},  # rebuilt later; its memory's promotion wins
            {"uuid": "u2", "name": "new-001", "source_description": "x | memory_id=m2",
             "created_at": "2026-09-13T00:00:00+00:00"},
            {"uuid": "u3", "name": "direct-remember", "created_at": "2026-09-20T00:00:00+00:00"},  # no memory id
            {"uuid": "u4", "name": "direct-old", "created_at": "2026-09-01T00:00:00+00:00"},
        ]
        g, r = FakeGraphiti(episodes), FakeRedis()
        result = await snap.snapshot_graph(T, "replay-t", journal_db=journal, redis_client=r, graphiti=g)
        self.assertEqual(g.removed, ["u2", "u3"])
        self.assertEqual(result.episodes_removed, 2)
        self.assertEqual(result.invalidated_facts, 1)
        self.assertEqual(r.commands[0], ("GRAPH.COPY", "mem-fabric-local", "replay-t"))
        self.assertNotIn(("GRAPH.DELETE", "mem-fabric-local"), r.commands)

    async def test_existing_snapshot_is_rebuilt(self):
        journal = _journal([])
        r = FakeRedis(); r.graphs.add("replay-t")
        await snap.snapshot_graph(T, "replay-t", journal_db=journal, redis_client=r, graphiti=FakeGraphiti([]))
        self.assertEqual(r.commands[0], ("GRAPH.DELETE", "replay-t"))


class TestWikiExport(unittest.TestCase):
    def test_exports_the_commit_before_as_of(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wiki"
            repo.mkdir()
            git = lambda *a, date: subprocess.run(
                ["git", "-C", str(repo), *a], check=True, capture_output=True,
                env={**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
                     "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"})
            git("init", "-q", date="2026-09-01T00:00:00Z")
            (repo / "note.md").write_text("old")
            git("add", ".", date="2026-09-01T00:00:00Z"); git("commit", "-qm", "a", date="2026-09-01T00:00:00Z")
            (repo / "note.md").write_text("new")
            git("commit", "-qam", "b", date="2026-09-20T00:00:00Z")
            dest = Path(tmp) / "out"
            self.assertIsNotNone(snap.export_wiki_at(repo, T, dest))
            self.assertEqual((dest / "note.md").read_text(), "old")
            self.assertEqual((repo / "note.md").read_text(), "new")  # repo untouched
            self.assertIsNone(snap.export_wiki_at(repo, datetime(2020, 1, 1, tzinfo=timezone.utc), Path(tmp) / "none"))


PROMOTED = {"gold-1": "2026-09-09T16:30:00+00:00", "later-1": "2026-09-11T00:00:00+00:00", "other": "2026-09-09T17:00:00+00:00"}
CASE = {"id": "A1", "query": "q", "gold_episodes": ["gold-1"], "gold_wiki": ["WIKI/x.md"]}


class TestGrading(unittest.TestCase):
    def test_rank_leak_provenance_superseded(self):
        facts = [{"episode_names": ["other"]}, {"episode_names": ["later-1"]},
                 {"episode_names": ["gold-1"], "invalid_at": "2026-09-12"}, {}]
        g = grade_case(CASE, "t", "p", facts, ["WIKI/y.md", "WIKI/x.md"], PROMOTED, "2026-09-10T00:00:00+00:00")
        self.assertEqual((g.memory_rank, g.wiki_rank, g.temporal_leaks, g.superseded_returned), (3, 2, 1, 1))
        self.assertEqual(g.provenance_rate, 0.75)
        self.assertTrue(g.gold_available)
        self.assertIsNone(g.abstained_correctly)

    def test_before_the_gold_existed_abstention_is_graded(self):
        g = grade_case(CASE, "t0", "p", [{"episode_names": ["other"]}], [], PROMOTED, "2026-09-09T16:00:00+00:00")
        self.assertFalse(g.gold_available)
        self.assertTrue(g.abstained_correctly)
        self.assertEqual(g.temporal_leaks, 1)  # "other" postdates 16:00 too

    def test_now_has_no_cutoff(self):
        g = grade_case(CASE, "now", "p", [{"episode_names": ["later-1"]}], [], PROMOTED, None)
        self.assertEqual(g.temporal_leaks, 0)
        self.assertTrue(g.gold_available)

    def test_summary(self):
        a = grade_case(CASE, "t", "p", [{"episode_names": ["gold-1"]}], ["WIKI/x.md"], PROMOTED, None)
        b = grade_case({**CASE, "id": "A2"}, "t", "p", [], [], PROMOTED, None)
        s = summarize([a, b], k=8)
        self.assertEqual((s["memory_answerable"], s["memory_hit_at_k"], s["memory_mrr"]), (2, 0.5, 0.5))


class TestPolicies(unittest.TestCase):
    def test_applied_sets_and_restores_env(self):
        with mock.patch.dict(os.environ, {"CMF_MEM_EPISODE_VECTOR": "1"}):
            with applied(EDGE_ONLY):
                self.assertEqual(os.environ["CMF_MEM_EPISODE_VECTOR"], "0")
            self.assertEqual(os.environ["CMF_MEM_EPISODE_VECTOR"], "1")
        env = {k: v for k, v in os.environ.items() if k != "CMF_MEM_EPISODE_VECTOR"}
        with mock.patch.dict(os.environ, env, clear=True):
            with applied(EDGE_PLUS_EPISODE_VECTOR):
                self.assertEqual(os.environ["CMF_MEM_EPISODE_VECTOR"], "1")
            self.assertNotIn("CMF_MEM_EPISODE_VECTOR", os.environ)


class TestPolicyKnobs(unittest.TestCase):
    def test_sweep_policy_sets_vector_k_and_episode_cap_then_restores(self):
        from server.providers import memory_graphiti as mg
        from server.replay.policies import SWEEP_POLICIES

        k0, merge0 = mg._EPISODE_VECTOR_K, mg._rrf_merge
        facts = [{"fact": f"f{i}", "episodes": ["e1"]} for i in range(3)] + [{"fact": "g", "episodes": ["e2"]}]
        with applied(SWEEP_POLICIES["vector-k3-cap1"]):
            self.assertEqual(mg._EPISODE_VECTOR_K, 3)
            self.assertEqual([f["fact"] for f in mg._rrf_merge(facts, limit=8)], ["f0", "g"])
        self.assertEqual((mg._EPISODE_VECTOR_K, mg._rrf_merge), (k0, merge0))
        self.assertEqual(len(merge0(facts, limit=8)), 2)  # production cap is 1 per episode
        self.assertEqual(len(merge0(facts, limit=8, max_per_episode=2)), 3)
        with applied(SWEEP_POLICIES["vector-k6-cap2"]):
            self.assertEqual(len(mg._rrf_merge(facts, limit=8)), 3)


class TestQueryTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_pins_a_default_timeout_and_restores_the_client(self):
        from falkordb.asyncio.graph import AsyncGraph
        from server.replay.runner import falkordb_query_timeout

        seen = []

        async def fake_query(self, q, params=None, timeout=None):
            seen.append(timeout)

        with mock.patch.object(AsyncGraph, "query", fake_query):
            with falkordb_query_timeout(30000):
                await AsyncGraph.query(None, "q")
                await AsyncGraph.query(None, "q", timeout=5)
            self.assertIs(AsyncGraph.query, fake_query)
            with falkordb_query_timeout(0):
                self.assertIs(AsyncGraph.query, fake_query)
        self.assertEqual(seen, [30000, 5])


class TestExport(unittest.TestCase):
    def test_trajectories_split_out_and_text_free_by_default(self):
        report = {"k": 8, "runs": [{"x": 1}], "trajectories": [{"case_id": "A1", "memory": [{"rank": 1, "episode_names": ["e"]}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            out, traj = write_report(report, Path(tmp) / "r.json")
            self.assertNotIn("trajectories", json.loads(out.read_text()))
            line = json.loads(traj.read_text().splitlines()[0])
            self.assertNotIn("fact", line["memory"][0])


class TestRunner(unittest.IsolatedAsyncioTestCase):
    async def test_wiki_search_reused_across_policies_but_not_snapshots(self):
        cases = [
            {"id": "B1", "query": "shared", "gold_wiki": ["WIKI/old.md", "WIKI/new.md"]},
            {"id": "B2", "query": "empty", "gold_wiki": ["WIKI/missing.md"]},
            {"id": "B3", "query": "shared", "gold_wiki": ["WIKI/old.md", "WIKI/new.md"]},
        ]
        snapshots = [Snapshot("early", "replay-early", Path("early-wiki"), T.isoformat()),
                     Snapshot("now", "replay-now", Path("now-wiki"), None)]
        indexes = []

        def make_index(root):
            path = "WIKI/old.md" if root == Path("early-wiki") else "WIKI/new.md"
            index = mock.Mock()
            index.search.side_effect = lambda query, k: [SimpleNamespace(relative_path=path)] if query == "shared" else []
            indexes.append(index)
            return index

        with (mock.patch("server.replay.runner._WikiIndex", side_effect=make_index),
              mock.patch("server.replay.runner.first_promotion_by_memory", return_value={}),
              mock.patch("server.replay.runner.graph_episode_rows") as graph_read,
              mock.patch("server.providers.memory_graphiti.recall_mem", new_callable=mock.AsyncMock, return_value=[]) as recall,
              mock.patch("server.providers.memory_graphiti.close_graphiti", new_callable=mock.AsyncMock) as close,
              mock.patch("builtins.print") as progress):
            report = await run_replay(cases, snapshots, [EDGE_ONLY, EDGE_PLUS_EPISODE_VECTOR],
                                      Path("unused-journal"), "unused-source", k=3, episode_rows=[])

        graph_read.assert_not_called()
        self.assertEqual(recall.await_count, 12)  # Memory still runs for every case/policy/snapshot.
        self.assertEqual(close.await_count, 4)
        for index in indexes:
            self.assertEqual(index.search.call_args_list, [mock.call("shared", 3), mock.call("empty", 3)])
        self.assertEqual(len(indexes), 2)
        self.assertEqual(len(report["trajectories"]), 12)
        for trajectory in report["trajectories"]:
            path = "WIKI/old.md" if trajectory["snapshot"] == "early" else "WIKI/new.md"
            expected = [] if trajectory["case_id"] == "B2" else [{"rank": 1, "path": path}]
            self.assertEqual(trajectory["wiki"], expected)
        for run in report["runs"]:
            self.assertEqual(run["summary"]["wiki_hit_at_k"], 2 / 3)
        self.assertEqual(progress.call_args_list, [
            mock.call(f"replay finished: snapshot={snap.label} policy={policy.name} cases=3", flush=True)
            for snap in snapshots for policy in (EDGE_ONLY, EDGE_PLUS_EPISODE_VECTOR)
        ])

    async def test_progress_emitted_before_next_policy_and_not_for_failed_run(self):
        cases = [{"id": "A1", "query": "private question", "gold_episodes": []}]
        snapshot = Snapshot("now", "replay-now", None, None)
        with (mock.patch("server.replay.runner.first_promotion_by_memory", return_value={}),
              mock.patch("server.providers.memory_graphiti.recall_mem", new_callable=mock.AsyncMock) as recall,
              mock.patch("server.providers.memory_graphiti.close_graphiti", new_callable=mock.AsyncMock),
              mock.patch("builtins.print") as progress,
              mock.patch.dict(os.environ, {"FALKORDB_DATABASE": "original"})):
            async def retrieve(*args, **kwargs):
                if recall.await_count == 2:
                    progress.assert_called_once_with(
                        "replay finished: snapshot=now policy=edge-only cases=1", flush=True)
                    raise RuntimeError("retrieval failed")
                progress.assert_not_called()
                return []

            recall.side_effect = retrieve
            with self.assertRaisesRegex(RuntimeError, "retrieval failed"):
                await run_replay(cases, [snapshot], [EDGE_ONLY, EDGE_PLUS_EPISODE_VECTOR],
                                 Path("unused-journal"), "unused-source", episode_rows=[])
            self.assertEqual(os.environ["FALKORDB_DATABASE"], "original")
            self.assertEqual(progress.call_count, 1)


class TestAvailability(unittest.TestCase):
    def test_first_promotion_follows_the_lineage_and_ignores_experiment_graphs(self):
        journal = _journal([
            ("m1", "gemini-x-001", "mem-fabric-gemini", "2026-09-08T12:00:00+00:00"),  # renamed at the flip
            ("m1", "gemini-x-002", "mem-fabric-local", "2026-09-09T17:00:00+00:00"),
            ("m2", "y-001", "mem-fabric-local-glm", "2026-09-09T01:00:00+00:00"),  # A/B only, never production
            ("m2", "y-001", "mem-fabric-local", "2026-09-09T18:00:00+00:00"),
            ("m3", "z-001", "mem-fabric-local", "2026-09-20T00:00:00+00:00"),
        ])
        self.assertEqual(snap.first_promotion_by_memory(journal), {
            "m1": "2026-09-08T12:00:00+00:00",
            "m2": "2026-09-09T18:00:00+00:00",
            "m3": "2026-09-20T00:00:00+00:00",
        })

    def test_episodes_are_dated_by_their_own_memory_id_not_their_name(self):
        first = {"m1": "2026-09-08T12:00:00+00:00", "m2": "2026-09-11T00:00:00+00:00"}
        rows = [
            {"name": "gemini-x-001", "source_description": "Promoted ... | memory_id=m2", "created_at": None},
            {"name": "gemini-x-002", "source_description": "Promoted ... | memory_id=m1", "created_at": None},
            {"name": "direct", "source_description": "remember()", "created_at": "2026-09-15T01:02:03Z"},
            {"name": "unknown", "source_description": None, "created_at": None},
        ]
        self.assertEqual(snap.availability(rows, first), {
            "gemini-x-001": "2026-09-11T00:00:00+00:00",
            "gemini-x-002": "2026-09-08T12:00:00+00:00",
            "direct": "2026-09-15T01:02:03+00:00",
        })
        self.assertEqual(snap.memory_names(rows), {"m2": "gemini-x-001", "m1": "gemini-x-002"})


class TestResolveGold(unittest.TestCase):
    def test_memory_ids_win_over_stale_names_and_updates_resolve_too(self):
        from server.replay.runner import resolve_gold

        cases = [{"id": "A9", "gold_episodes": ["gemini-finance-001"], "gold_memory_ids": ["m-schedule-c"]},
                 {"id": "B1", "gold_episodes": [], "gold_wiki": ["WIKI/x.md"]},
                 {"id": "C6", "gold_episodes": ["ev-002"], "gold_memory_ids": ["m-ev2", "m-gone"]}]
        names = {"m-schedule-c": "gemini-finance-002", "m-ev2": "ev-002", "m-ev14": "ev-014"}
        updates = {"C6": [{"from": "2026-09-11T00:00:00+00:00", "add_memory_ids": ["m-ev14"],
                           "add": ["ev_second_engineering_review"], "retire_memory_ids": ["m-ev2"]}]}
        out, problems = resolve_gold(cases, names, updates)
        self.assertEqual(out[0]["gold_episodes"], ["gemini-finance-002"])
        self.assertEqual(out[1], cases[1])
        self.assertEqual(out[2]["gold_episodes"], ["ev-002"])
        self.assertEqual(out[2]["gold_updates"], [{"from": "2026-09-11T00:00:00+00:00",
                                                    "add": ["ev-014", "ev_second_engineering_review"],
                                                    "retire": ["ev-002"]}])
        self.assertEqual(problems, ["C6: 1 gold memory id(s) not in the graph"])
        self.assertNotIn("gold_updates", resolve_gold(cases, names)[0][2])


class TestWikiAvailability(unittest.TestCase):
    def test_a_note_written_after_the_cutoff_is_left_out_of_wiki_hits(self):
        case = {"id": "B18", "gold_episodes": [], "gold_wiki": ["WIKI/late.md"]}
        early = grade_case(case, "t", "p", [], ["WIKI/x.md"], {}, "2026-09-10T00:00:00+00:00",
                           frozenset(), frozenset({"WIKI/x.md"}))
        later = grade_case(case, "t", "p", [], ["WIKI/late.md"], {}, "2026-09-20T00:00:00+00:00",
                           frozenset(), frozenset({"WIKI/x.md", "WIKI/late.md"}))
        live = grade_case(case, "now", "p", [], ["WIKI/late.md"], {}, None)
        self.assertEqual((early.wiki_gold_available, later.wiki_gold_available, live.wiki_gold_available),
                         (False, True, True))
        self.assertIsNone(grade_case({**case, "gold_wiki": []}, "t", "p", [], [], {}, None).wiki_gold_available)
        s = summarize([early, later], k=8)
        self.assertEqual((s["wiki_answerable"], s["wiki_hit_at_k"]), (1, 1.0))


class TestEffectiveGold(unittest.TestCase):
    CASE = {"id": "C6", "gold_episodes": ["old"], "gold_wiki": [],
            "gold_updates": [{"from": "2026-09-11T00:00:00+00:00", "add": ["new"], "retire": ["old"]},
                             {"from": None, "add": ["missed"], "retire": []}]}
    PROMOTED = {"old": "2026-09-08T00:00:00+00:00", "new": "2026-09-11T00:00:00+00:00",
                "missed": "2026-09-08T00:00:00+00:00"}

    def test_gold_follows_the_cutoff(self):
        from server.replay.grading import effective_gold

        self.assertEqual(effective_gold(self.CASE, "2026-09-10T00:00:00+00:00"), {"old", "missed"})
        self.assertEqual(effective_gold(self.CASE, "2026-09-20T00:00:00+00:00"), {"new", "missed"})
        self.assertEqual(effective_gold(self.CASE, None), {"new", "missed"})

    def test_a_stale_answer_no_longer_counts_after_it_is_retired(self):
        facts = [{"episode_names": ["old"]}, {"episode_names": ["new"]}]
        early = grade_case(self.CASE, "t", "p", facts, [], self.PROMOTED, "2026-09-10T00:00:00+00:00")
        now = grade_case(self.CASE, "now", "p", facts, [], self.PROMOTED, None)
        self.assertEqual((early.memory_rank, now.memory_rank), (1, 2))


class RollbackDriver:
    """Fake driver for the invalidation-restore and note-pruning steps."""

    def __init__(self, episodes, expired=(), notes=(), note_entities=()):
        self.episodes, self.expired = episodes, list(expired)
        self.notes, self.note_entities = list(notes), list(note_entities)
        self.restored, self.notes_deleted, self.entities_checked = None, None, None

    async def execute_query(self, q, **p):
        if "RETURN e.uuid AS uuid" in q:
            return [[dict(e) for e in self.episodes]]
        if "SET r.expired_at = NULL" in q:
            self.restored = p["uuids"]
            return [[{"c": len(p["uuids"])}]]
        if "r.expired_at IS NOT NULL" in q:
            return [[dict(e) for e in self.expired]]
        if "RETURN n.note_path AS path" in q:
            # a note is a path, or (path, created_at) for a dated one
            return [[{"path": n, "created_at": None} if isinstance(n, str) else {"path": n[0], "created_at": n[1]}
                     for n in self.notes]]
        if "RETURN DISTINCT x.uuid" in q:
            return [[{"uuid": u} for u in self.note_entities]]
        if "n.note_path IN $paths" in q:
            self.notes_deleted = p["paths"]
            return [[{"c": len(p["paths"])}]]
        if "x.uuid IN $uuids" in q:
            self.entities_checked = p["uuids"]
            return [[{"c": len(p["uuids"])}]]
        if "RETURN eps" in q:
            return [[{"eps": 2, "ents": 5}]]
        return [[{"c": 0}]]


class RollbackGraphiti(FakeGraphiti):
    def __init__(self, driver):
        self.driver, self.removed = driver, []


class TestRollback(unittest.IsolatedAsyncioTestCase):
    EPISODES = [
        {"uuid": "u1", "name": "early", "source_description": "memory_id=m1", "created_at": "2026-09-13T10:00:00Z"},
        {"uuid": "u2", "name": "late", "source_description": "memory_id=m2", "created_at": "2026-09-13T10:05:00Z"},
        {"uuid": "u3", "name": "early-2", "source_description": "memory_id=m3", "created_at": "2026-09-13T10:10:00Z"},
    ]
    LEDGER = [
        ("m1", "early", "mem-fabric-local", "2026-09-08T00:00:00+00:00"),
        ("m2", "late", "mem-fabric-local", "2026-09-11T00:00:00+00:00"),
        ("m3", "early-2", "mem-fabric-local", "2026-09-09T00:00:00+00:00"),
    ]

    async def test_restores_only_invalidations_made_while_a_removed_episode_was_ingested(self):
        driver = RollbackDriver(self.EPISODES, expired=[
            {"uuid": "r1", "expired_at": "2026-09-13T10:03:00Z"},  # during early's ingestion: keep
            {"uuid": "r2", "expired_at": "2026-09-13T10:07:00Z"},  # during late's (removed): restore
            {"uuid": "r3", "expired_at": "2026-09-13T10:12:00Z"},  # during early-2's: keep
            {"uuid": "r4", "expired_at": "2026-09-13T09:00:00Z"},  # before any episode: keep
        ])
        g = RollbackGraphiti(driver)
        result = await snap.snapshot_graph(T, "replay-t", journal_db=_journal(self.LEDGER),
                                           redis_client=FakeRedis(), graphiti=g)
        self.assertEqual(g.removed, ["u2"])
        self.assertEqual(driver.restored, ["r2"])
        self.assertEqual(result.invalidations_restored, 1)

    async def test_prunes_notes_whose_file_did_not_exist_yet(self):
        driver = RollbackDriver(self.EPISODES, notes=["WIKI/a.md", "WIKI/new.md"], note_entities=["x1"])
        result = await snap.snapshot_graph(T, "replay-t", journal_db=_journal(self.LEDGER), redis_client=FakeRedis(),
                                           graphiti=RollbackGraphiti(driver), wiki_paths={"WIKI/a.md"})
        self.assertEqual(driver.notes_deleted, ["WIKI/new.md"])
        self.assertEqual(driver.entities_checked, ["x1"])
        self.assertEqual((result.notes_removed, result.note_entities_removed), (1, 1))

    async def test_undated_notes_left_alone_without_a_wiki_export(self):
        driver = RollbackDriver(self.EPISODES, notes=["WIKI/new.md"])
        await snap.snapshot_graph(T, "replay-t", journal_db=_journal(self.LEDGER), redis_client=FakeRedis(),
                                  graphiti=RollbackGraphiti(driver))
        self.assertIsNone(driver.notes_deleted)

    async def test_without_an_export_prunes_notes_created_after_as_of(self):
        driver = RollbackDriver(self.EPISODES, note_entities=["x1"], notes=[
            ("WIKI/old.md", "2026-04-22T14:29:30-05:00"),   # import upper bound, before T
            ("WIKI/later.md", "2026-09-10T00:00:01+00:00"),  # one second after T
            "WIKI/undated.md",
        ])
        result = await snap.snapshot_graph(T, "replay-t", journal_db=_journal(self.LEDGER), redis_client=FakeRedis(),
                                           graphiti=RollbackGraphiti(driver))
        self.assertEqual(driver.notes_deleted, ["WIKI/later.md"])
        self.assertEqual(result.notes_removed, 1)

    async def test_an_export_overrides_created_at(self):
        # created_at says WIKI/later.md is new, but the export shows it existed at as_of.
        driver = RollbackDriver(self.EPISODES, notes=[("WIKI/later.md", "2026-09-11T00:00:00+00:00"),
                                                      ("WIKI/gone.md", "2026-01-01T00:00:00+00:00")])
        await snap.snapshot_graph(T, "replay-t", journal_db=_journal(self.LEDGER), redis_client=FakeRedis(),
                                  graphiti=RollbackGraphiti(driver), wiki_paths={"WIKI/later.md"})
        self.assertEqual(driver.notes_deleted, ["WIKI/gone.md"])


class TestWikiWindow(unittest.TestCase):
    def test_window_brackets_the_cutoff_and_lists_uncertain_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wiki"
            repo.mkdir()

            def commit(date, files, msg=None):
                env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
                       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                       "GIT_COMMITTER_EMAIL": "t@x"}
                for name, text in files.items():
                    (repo / name).write_text(text)
                subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
                subprocess.run(["git", "-C", str(repo), "commit", "-qm", msg or date], check=True, env=env)

            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            commit("2026-09-01T00:00:00Z", {"note.md": "old", "same.md": "x"})
            commit("2026-09-20T00:00:00Z", {"note.md": "new", "added.md": "y"})
            w = snap.wiki_window(repo, T)
            self.assertEqual(sorted(w.uncertain_paths), ["added.md", "note.md"])
            self.assertEqual(w.gap_hours, 19 * 24)
            last = snap.wiki_window(repo, datetime(2026, 9, 25, tzinfo=timezone.utc))
            self.assertIsNone(last.next_commit)
            self.assertEqual(last.uncertain_paths, [])
            dest = Path(tmp) / "out"
            snap.export_wiki_at(repo, T, dest)
            self.assertEqual(snap.exported_paths(dest), {"note.md", "same.md"})
            # A watcher's auto-commit well after the cut-off pins the change after it...
            commit("2026-09-22T12:00:00Z", {"late.md": "z"}, msg="Auto-sync 2026-09-22 12:00:00")
            auto = snap.wiki_window(repo, datetime(2026, 9, 21, tzinfo=timezone.utc))
            self.assertTrue(auto.next_is_auto)
            self.assertEqual(auto.uncertain_paths, [])
            # ...but not one within the commit latency of it.
            near = snap.wiki_window(repo, datetime(2026, 9, 22, 11, 58, tzinfo=timezone.utc))
            self.assertEqual(near.uncertain_paths, ["late.md"])


class TestWikiUncertainGrade(unittest.TestCase):
    def test_flags_gold_wiki_changed_inside_the_window(self):
        uncertain = frozenset({"WIKI/x.md"})
        g = grade_case(CASE, "t", "p", [], ["WIKI/x.md"], PROMOTED, "2026-09-10T00:00:00+00:00", uncertain)
        self.assertTrue(g.wiki_uncertain)
        self.assertFalse(grade_case(CASE, "t", "p", [], [], PROMOTED, None).wiki_uncertain)
        memory_only = {**CASE, "gold_wiki": []}
        self.assertIsNone(grade_case(memory_only, "t", "p", [], [], PROMOTED, None, uncertain).wiki_uncertain)
        self.assertEqual(summarize([g], k=8)["wiki_uncertain_cases"], 1)


if __name__ == "__main__":
    unittest.main()
