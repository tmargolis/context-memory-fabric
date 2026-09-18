"""Tests for MS4a MCP-boundary capture (server.capture.*).

Uses lightweight fakes for the MCP session/client_info shapes rather than a
real MCPServer connection — the middleware only touches a small, documented
surface (session.client_params.client_info, session._connection.session_id,
ctx.method/params/request_id), so faking exactly that surface keeps these
tests fast and independent of the mcp package's internals.
"""

import asyncio
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from server.capture import filters, identity
from server.capture.health import CaptureHealth
from server.capture.middleware import CaptureMiddleware, build_tool_call_event
from server.journal.store import SqliteEventStore


class _FakeClientInfo:
    """Mimics mcp_types.Implementation's shape. A plain object (not
    SimpleNamespace) so it supports weak references, matching real
    ServerSession/Implementation instances — SimpleNamespace does not,
    which would mask identity.resolve_session_id's caching behavior."""

    def __init__(self, name, version="1.0.0"):
        self.name = name
        self.version = version


class _FakeClientParams:
    def __init__(self, client_info):
        self.client_info = client_info


class _FakeConnection:
    def __init__(self, session_id=None):
        self.session_id = session_id


class _FakeSession:
    def __init__(self, client_params=None, connection=None):
        self.client_params = client_params
        self._connection = connection


def fake_client_info(name, version="1.0.0"):
    return _FakeClientInfo(name, version)


def fake_session(client_info=None, native_session_id=None):
    client_params = _FakeClientParams(client_info) if client_info is not None else None
    connection = _FakeConnection(native_session_id)
    return _FakeSession(client_params=client_params, connection=connection)


class TestResolveHarness(unittest.TestCase):
    def test_known_clients_normalize(self):
        cases = {
            "Claude Desktop": "claude_desktop",
            "claude-desktop": "claude_desktop",
            "claude-code": "claude_code",
            "ChatGPT": "chatgpt",
            "Cursor": "cursor",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(identity.resolve_harness(fake_client_info(raw)), expected)

    def test_unknown_client_falls_back_to_slug(self):
        result = identity.resolve_harness(fake_client_info("Some New Tool v2"))
        self.assertEqual(result, "some_new_tool_v2")

    def test_missing_client_info_is_safe(self):
        self.assertEqual(identity.resolve_harness(None), "unknown_mcp_client")

    def test_client_info_without_name_is_safe(self):
        self.assertEqual(identity.resolve_harness(SimpleNamespace()), "unknown_mcp_client")


class TestResolveSessionId(unittest.TestCase):
    def test_prefers_native_session_id_when_present(self):
        session = fake_session(native_session_id="abc123")
        self.assertEqual(identity.resolve_session_id(session), "native:abc123")

    def test_synthesizes_and_caches_when_no_native_id(self):
        session = fake_session(native_session_id=None)
        first = identity.resolve_session_id(session)
        second = identity.resolve_session_id(session)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("cmf:"))

    def test_different_sessions_get_different_synthesized_ids(self):
        session_a = fake_session(native_session_id=None)
        session_b = fake_session(native_session_id=None)
        self.assertNotEqual(identity.resolve_session_id(session_a), identity.resolve_session_id(session_b))


class TestRedactSecrets(unittest.TestCase):
    def test_redacts_by_key_name_regardless_of_value_shape(self):
        result = filters.redact_secrets({"api_key": "not-a-recognized-shape-but-named-like-one"})
        self.assertEqual(result["api_key"], filters.REDACTED)

    def test_redacts_google_api_key_shaped_value_under_any_key(self):
        # Same shape as GEMINI_API_KEY: "AIza" + 35 chars. A fake value, not
        # the real key, matching MS0.5's live-fixture intent without
        # embedding a real secret in the test suite.
        fake_key = "AIza" + "x" * 35
        result = filters.redact_secrets({"notes": f"here is a key: {fake_key}"})
        self.assertNotIn(fake_key, result["notes"])

    def test_leaves_ordinary_text_untouched(self):
        result = filters.redact_secrets({"content": "Decided to use PostgreSQL for the task queue."})
        self.assertEqual(result["content"], "Decided to use PostgreSQL for the task queue.")

    def test_recurses_into_nested_structures(self):
        result = filters.redact_secrets({"outer": {"inner_list": [{"token": "abc"}]}})
        self.assertEqual(result["outer"]["inner_list"][0]["token"], filters.REDACTED)

    def test_redacted_count_reflects_actual_redactions(self):
        fake_key = "AIza" + "y" * 35
        redacted, count = filters.redact_secrets_and_count({"api_key": "whatever", "note": fake_key, "safe": "hello"})
        self.assertEqual(count, 2)
        self.assertEqual(redacted["safe"], "hello")


class TestShouldCapture(unittest.TestCase):
    def test_denies_bulk_import_tools_by_default(self):
        self.assertFalse(filters.should_capture("import_memories", "claude_desktop"))
        self.assertFalse(filters.should_capture("import_chatgpt_exports", "claude_desktop"))

    def test_allows_ordinary_tools_by_default(self):
        self.assertTrue(filters.should_capture("remember", "claude_desktop"))
        self.assertTrue(filters.should_capture("recall", "claude_code"))


class TestBuildToolCallEvent(unittest.TestCase):
    def test_builds_event_with_expected_shape(self):
        session = fake_session(client_info=fake_client_info("Claude Desktop"), native_session_id=None)
        built = build_tool_call_event(
            tool_name="remember",
            arguments={"content": "Decided to use SQLite."},
            result="Memory stored successfully.",
            session=session,
            request_id="req-1",
        )
        self.assertIsNotNone(built)
        event, redacted_count = built
        self.assertEqual(event.event_type, "mcp_tool_call")
        self.assertEqual(event.source.harness, "claude_desktop")
        self.assertEqual(event.actor_type, "user")
        self.assertEqual(event.content["tool"], "remember")
        self.assertEqual(redacted_count, 0)

    def test_returns_none_for_denied_tool(self):
        session = fake_session(client_info=fake_client_info("Claude Desktop"))
        built = build_tool_call_event(
            tool_name="import_memories",
            arguments={},
            result="ok",
            session=session,
            request_id="req-2",
        )
        self.assertIsNone(built)

    def test_secret_shaped_argument_never_reaches_the_event(self):
        fake_key = "AIza" + "z" * 35
        session = fake_session(client_info=fake_client_info("Claude Desktop"))
        built = build_tool_call_event(
            tool_name="remember",
            arguments={"content": f"my key is {fake_key}"},
            result="ok",
            session=session,
            request_id="req-3",
        )
        event, _ = built
        self.assertNotIn(fake_key, str(event.content))

    def test_same_tool_and_content_in_two_requests_gets_distinct_event_ids(self):
        # Two genuinely separate calls with identical arguments must not
        # collapse into one journaled event via content-hash-only dedup.
        session = fake_session(client_info=fake_client_info("Claude Desktop"))
        built1 = build_tool_call_event(
            tool_name="recall", arguments={"query": "x"}, result="r", session=session, request_id="req-a"
        )
        built2 = build_tool_call_event(
            tool_name="recall", arguments={"query": "x"}, result="r", session=session, request_id="req-b"
        )
        self.assertNotEqual(built1[0].event_id, built2[0].event_id)


class TestCaptureMiddleware(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "journal.db"
        self.store = SqliteEventStore(db_path=self.db_path)
        self.health = CaptureHealth()
        self.middleware = CaptureMiddleware(event_store=self.store, health=self.health, queue_maxsize=5)

    async def asyncTearDown(self):
        self.store.close()
        self.tmp_dir.cleanup()

    def _ctx(self, method="tools/call", params=None, request_id="req-1", session=None):
        return SimpleNamespace(
            method=method,
            params=params,
            request_id=request_id,
            session=session or fake_session(client_info=fake_client_info("Claude Desktop")),
        )

    async def test_call_next_result_is_returned_unchanged(self):
        async def call_next(ctx):
            return "the real result"

        ctx = self._ctx(params={"name": "remember", "arguments": {"content": "x"}})
        result = await self.middleware(ctx, call_next)
        self.assertEqual(result, "the real result")

    async def test_non_tool_call_methods_are_not_captured(self):
        async def call_next(ctx):
            return {"ok": True}

        ctx = self._ctx(method="resources/list", params={})
        await self.middleware(ctx, call_next)
        await asyncio.sleep(0.05)
        self.assertEqual(self.health.snapshot()["captured"], 0)

    async def test_tool_call_is_captured_and_journaled(self):
        async def call_next(ctx):
            return "stored"

        ctx = self._ctx(params={"name": "remember", "arguments": {"content": "Decided X."}})
        await self.middleware(ctx, call_next)
        # Capture is fire-and-forget (asyncio.create_task); give the loop a
        # tick to run it and the consumer to drain the queue.
        await asyncio.sleep(0.1)

        snapshot = self.health.snapshot()
        self.assertEqual(snapshot["captured"], 1)
        stats = self.store.stats()
        self.assertEqual(stats["total_events"], 1)
        self.assertEqual(stats["by_harness"], {"claude_desktop": 1})

    async def test_denied_tool_is_not_journaled(self):
        async def call_next(ctx):
            return "report"

        ctx = self._ctx(params={"name": "import_memories", "arguments": {}})
        await self.middleware(ctx, call_next)
        await asyncio.sleep(0.1)

        self.assertEqual(self.store.stats()["total_events"], 0)

    async def test_full_queue_drops_and_counts_rather_than_blocking(self):
        async def call_next(ctx):
            return "ok"

        # queue_maxsize=5; fire 20 distinct captures rapidly before the
        # consumer (a separate task) gets a chance to drain any of them, by
        # not yielding between put_nowait calls at all — build events
        # directly and enqueue synchronously to make the race deterministic
        # rather than timing-dependent.
        for i in range(20):
            ctx = self._ctx(params={"name": "recall", "arguments": {"query": f"q{i}"}}, request_id=f"req-{i}")
            await self.middleware(ctx, call_next)

        await asyncio.sleep(0.2)
        snapshot = self.health.snapshot()
        # Some may have been dropped if the consumer didn't keep up, but
        # captured + dropped must account for all 20 attempts either way,
        # and the middleware must not have raised for any of them.
        self.assertEqual(snapshot["captured"] + snapshot["dropped"], 20)


if __name__ == "__main__":
    unittest.main()
