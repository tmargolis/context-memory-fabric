"""Tests for the Milestone 2 event journal: identity, store, retention.

Covers IMPLEMENTATION-PLAN.md's Milestone 2 acceptance tests 1 (idempotent
re-import), 2 (deterministic replay), and 6 (retention policy), at the
journal-primitive level — importer-level coverage lives in
tests/test_ms2_importers.py.
"""

from datetime import datetime, timezone
import json
import tempfile
import unittest
from pathlib import Path

from server.core.models import DatePrecision, SourceEvent, SourceProvenance
from server.journal.cli import build_parser
from server.journal.identity import compute_content_hash, compute_event_id
from server.journal.retention import RetentionClass, RetentionPolicy
from server.journal.store import SqliteEventStore


def make_event(event_id="chatgpt:conv1:msg1", content=None, **overrides):
    content = content if content is not None else {"text": "hello world"}
    defaults = dict(
        schema_version="1.0",
        event_id=event_id,
        event_type="turn.completed",
        source=SourceProvenance(harness="chatgpt", conversation_id="conv1", turn_id="msg1"),
        observed_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
        content=content,
        content_hash=compute_content_hash(content),
        date_precision=DatePrecision.NONE,
    )
    defaults.update(overrides)
    return SourceEvent(**defaults)


class TestContentHash(unittest.TestCase):
    def test_identical_content_hashes_identically(self):
        a = compute_content_hash({"text": "hello"})
        b = compute_content_hash({"text": "hello"})
        self.assertEqual(a, b)

    def test_whitespace_near_miss_hashes_identically(self):
        a = compute_content_hash({"text": "hello world"})
        b = compute_content_hash({"text": "  hello world  "})
        self.assertEqual(a, b, "Trailing/leading whitespace must not change identity")

    def test_key_order_does_not_affect_hash(self):
        a = compute_content_hash({"text": "hi", "heading": "Section 1"})
        b = compute_content_hash({"heading": "Section 1", "text": "hi"})
        self.assertEqual(a, b)

    def test_different_content_hashes_differently(self):
        a = compute_content_hash({"text": "hello"})
        b = compute_content_hash({"text": "goodbye"})
        self.assertNotEqual(a, b)

    def test_hash_format_matches_schema_pattern(self):
        h = compute_content_hash({"text": "x"})
        self.assertTrue(h.startswith("sha256:"))
        self.assertEqual(len(h), len("sha256:") + 64)


class TestEventIdentity(unittest.TestCase):
    def test_native_id_preferred_over_content_hash(self):
        event_id = compute_event_id("chatgpt", "sha256:aaaa", conversation_id="conv1", turn_id="msg1")
        self.assertEqual(event_id, "chatgpt:conv1:msg1")

    def test_falls_back_to_content_hash_when_no_native_id(self):
        h = compute_content_hash({"text": "hi"})
        event_id = compute_event_id("chatgpt", h)
        self.assertEqual(event_id, f"chatgpt:{h}")

    def test_turn_id_alone_without_conversation_id_is_not_trusted(self):
        h = compute_content_hash({"text": "hi"})
        event_id = compute_event_id("chatgpt", h, turn_id="msg1")
        self.assertNotIn("msg1", event_id, "A bare turn_id is not globally unique on its own")

    def test_namespace_disambiguates_fallback_form(self):
        h = compute_content_hash({"text": "hi"})
        event_id = compute_event_id("chatgpt", h, namespace="backfill")
        self.assertTrue(event_id.startswith("chatgpt:backfill:"))


class TestSqliteEventStoreIdempotency(unittest.TestCase):
    """Milestone 2 acceptance test 1: importing the same export twice
    produces zero new events.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "journal.db"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_appending_the_same_event_twice_is_a_noop(self):
        with SqliteEventStore(self.db_path) as store:
            event = make_event()
            first = store.append(event)
            second = store.append(event)
            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(store.stats()["total_events"], 1)

    def test_appending_two_distinct_events_both_persist(self):
        with SqliteEventStore(self.db_path) as store:
            store.append(make_event(event_id="chatgpt:conv1:msg1"))
            store.append(make_event(event_id="chatgpt:conv1:msg2"))
            self.assertEqual(store.stats()["total_events"], 2)

    def test_reopening_the_store_preserves_data(self):
        with SqliteEventStore(self.db_path) as store:
            store.append(make_event())
        with SqliteEventStore(self.db_path) as store:
            self.assertEqual(store.stats()["total_events"], 1)
            self.assertIsNotNone(store.get("chatgpt:conv1:msg1"))

    def test_get_roundtrips_all_fields(self):
        with SqliteEventStore(self.db_path) as store:
            original = make_event(
                event_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
                date_precision=DatePrecision.YEAR,
                actor_type="assistant",
                parent_event_ids=["chatgpt:conv1:msg0"],
                metadata={"sender": "assistant"},
            )
            store.append(original)
            fetched = store.get(original.event_id)
            self.assertEqual(fetched.event_id, original.event_id)
            self.assertEqual(fetched.content, original.content)
            self.assertEqual(fetched.event_date, original.event_date)
            self.assertEqual(fetched.date_precision, DatePrecision.YEAR)
            self.assertEqual(fetched.actor_type, "assistant")
            self.assertEqual(fetched.parent_event_ids, ["chatgpt:conv1:msg0"])
            self.assertEqual(fetched.metadata, {"sender": "assistant"})

    def test_query_filters_by_harness_and_conversation(self):
        with SqliteEventStore(self.db_path) as store:
            store.append(make_event(event_id="chatgpt:conv1:msg1"))
            store.append(
                make_event(
                    event_id="claude:conv2:msg1",
                    source=SourceProvenance(harness="claude", conversation_id="conv2", turn_id="msg1"),
                )
            )
            chatgpt_events = store.query(harness="chatgpt")
            self.assertEqual(len(chatgpt_events), 1)
            self.assertEqual(chatgpt_events[0].source.harness, "chatgpt")


class TestReplayDeterminism(unittest.TestCase):
    """Milestone 2 acceptance test 2: journal replay with a pinned
    normalization policy is byte-identical across runs.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "journal.db"
        with SqliteEventStore(self.db_path) as store:
            store.append(make_event(event_id="chatgpt:conv1:msg1", content={"text": "first"}))
            store.append(
                make_event(
                    event_id="chatgpt:conv1:msg2",
                    content={"text": "second"},
                    observed_at=datetime(2026, 9, 3, 1, tzinfo=timezone.utc),
                )
            )

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_replay_twice_produces_byte_identical_output(self):
        out1 = Path(self._tmpdir.name) / "replay1.jsonl"
        out2 = Path(self._tmpdir.name) / "replay2.jsonl"
        parser = build_parser()

        args1 = parser.parse_args(["--db", str(self.db_path), "replay", "--out", str(out1)])
        args1.func(args1)
        args2 = parser.parse_args(["--db", str(self.db_path), "replay", "--out", str(out2)])
        args2.func(args2)

        self.assertEqual(out1.read_bytes(), out2.read_bytes())
        self.assertEqual(len(out1.read_text().strip().splitlines()), 2)


class TestRetentionPolicy(unittest.TestCase):
    """Milestone 2 acceptance test 6: a content class marked `excluded`
    never reaches the store; one marked `redacted` stores the redacted
    form only.
    """

    def test_default_policy_passes_content_through_raw(self):
        policy = RetentionPolicy()
        content = {"text": "ordinary content"}
        self.assertEqual(policy.classify(), RetentionClass.RAW)
        self.assertEqual(policy.apply(content), content)

    def test_excluded_content_class_returns_none(self):
        policy = RetentionPolicy(rules={"default": RetentionClass.RAW, "secret": RetentionClass.EXCLUDED})
        result = policy.apply({"text": "api key AQ.TESTONLY_not_a_real_key_0000000000000000"}, content_class="secret")
        self.assertIsNone(result)

    def test_excluded_event_never_reaches_the_store(self):
        policy = RetentionPolicy(rules={"default": RetentionClass.RAW, "secret": RetentionClass.EXCLUDED})
        content = policy.apply({"text": "leaked key"}, content_class="secret")
        with tempfile.TemporaryDirectory() as tmpdir:
            with SqliteEventStore(Path(tmpdir) / "journal.db") as store:
                if content is not None:
                    store.append(make_event(content=content))
                self.assertEqual(store.stats()["total_events"], 0)

    def test_redacted_content_class_scrubs_secret_shaped_strings(self):
        policy = RetentionPolicy(rules={"default": RetentionClass.RAW, "sensitive": RetentionClass.REDACTED})
        content = {"text": "Here is my key: AQ.TESTONLY_not_a_real_key_0000000000000000"}
        result = policy.apply(content, content_class="sensitive")
        self.assertNotIn("AQ.TESTONLY_not_a_real_key_0000000000000000", result["text"])
        self.assertIn("[REDACTED]", result["text"])

    def test_redacted_form_is_what_gets_stored(self):
        policy = RetentionPolicy(rules={"default": RetentionClass.RAW, "sensitive": RetentionClass.REDACTED})
        raw_content = {"text": "key sk-abcdefghijklmnopqrstuvwxyz123456"}
        stored_content = policy.apply(raw_content, content_class="sensitive")
        with tempfile.TemporaryDirectory() as tmpdir:
            with SqliteEventStore(Path(tmpdir) / "journal.db") as store:
                store.append(make_event(content=stored_content))
                fetched = store.get("chatgpt:conv1:msg1")
                self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", json.dumps(fetched.content))


if __name__ == "__main__":
    unittest.main()
