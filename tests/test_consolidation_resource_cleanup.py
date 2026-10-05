"""Consolidation doesn't leak sqlite connections or LLM HTTP clients.

Regression for the launchd poller's first 872-conversation run (2026-09-22):
50+ open handles on journal.db and a stream of "Event loop is closed" /
"Task exception was never retrieved" tracebacks. Two causes, one per test
class: run_reasoning_consolidation() opened a ThreadIndex and a ReviewStore
per call and never closed them, and _local_generate() built an LM Studio
client per window and left it for the garbage collector after its event
loop had already closed.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from server.consolidation import pipeline
from server.consolidation.store import ConsolidationStore
from server.consolidation.threads import ThreadIndex
from server.journal.store import SqliteEventStore
from server.policies import reasoning_episode
from server.policies.reasoning_episode import ReasoningEpisodePolicyV1
from server.review.store import ReviewStore


def _spy(cls):
    class Spy(cls):
        instances: list = []

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.closed = False
            Spy.instances.append(self)

        def close(self):
            self.closed = True
            super().close()

    return Spy


class TestPipelineClosesStoresItOpens(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.journal_path = Path(self._tmp.name) / "journal.db"
        self.cons_path = Path(self._tmp.name) / "consolidation.db"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, **kw):
        policy = ReasoningEpisodePolicyV1(generate_fn=lambda *a, **k: '{"episodes": []}')
        with SqliteEventStore(self.journal_path) as j, ConsolidationStore(self.cons_path) as c:
            return pipeline.run_reasoning_consolidation(j, c, policy, harness="claude_code", conversation_id="c1", **kw)

    def test_self_opened_stores_are_closed(self):
        ti_spy, rs_spy = _spy(ThreadIndex), _spy(ReviewStore)
        with mock.patch.object(pipeline, "ThreadIndex", ti_spy), mock.patch.object(pipeline, "ReviewStore", rs_spy):
            for _ in range(3):
                self._run()
        self.assertEqual(len(ti_spy.instances), 3)
        self.assertEqual(len(rs_spy.instances), 3)
        self.assertTrue(all(s.closed for s in ti_spy.instances + rs_spy.instances))

    def test_caller_passed_stores_are_left_open(self):
        ti = _spy(ThreadIndex)(self.cons_path)
        rs = _spy(ReviewStore)(self.cons_path)
        try:
            self._run(thread_index=ti, review_store=rs)
            self.assertFalse(ti.closed)
            self.assertFalse(rs.closed)
        finally:
            ti.close()
            rs.close()

    def test_stores_are_closed_when_the_run_raises(self):
        rs_spy = _spy(ReviewStore)
        with mock.patch.object(pipeline, "ReviewStore", rs_spy), mock.patch.object(
            pipeline, "group_by_conversation", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self._run()
        self.assertTrue(rs_spy.instances[0].closed)


class _FakeClient:
    instances: list = []

    def __init__(self, base_url, api_key):
        self.closed = False
        self.chat = mock.Mock()
        reply = mock.Mock()
        reply.choices = [mock.Mock(message=mock.Mock(content='{"episodes": []}'))]
        self.chat.completions.create = mock.AsyncMock(return_value=reply)
        _FakeClient.instances.append(self)

    async def close(self):
        self.closed = True


class TestLocalGenerateClosesItsClient(unittest.TestCase):
    def setUp(self):
        _FakeClient.instances = []

    def test_client_is_closed_after_each_call(self):
        with mock.patch("server.providers.lmstudio_client.LMStudioCompatClient", _FakeClient):
            for _ in range(2):
                self.assertEqual(reasoning_episode._local_generate("m", "prompt"), '{"episodes": []}')
        self.assertEqual(len(_FakeClient.instances), 2)
        self.assertTrue(all(c.closed for c in _FakeClient.instances))

    def test_client_is_closed_when_the_call_fails(self):
        def failing(base_url, api_key):
            client = _FakeClient(base_url, api_key)
            client.chat.completions.create = mock.AsyncMock(side_effect=ValueError("bad reply"))
            return client

        with mock.patch("server.providers.lmstudio_client.LMStudioCompatClient", failing):
            with self.assertRaises(ValueError):
                reasoning_episode._local_generate("m", "prompt")
        self.assertTrue(all(c.closed for c in _FakeClient.instances))


if __name__ == "__main__":
    unittest.main()
