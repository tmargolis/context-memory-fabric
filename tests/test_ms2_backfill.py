"""Milestone 2 backfill tests.

Covers acceptance tests 3 ("every one of the 57 production memories
resolves to a source event") and 5 ("re-imported default_db content
anchors event dates to occurrences, not to mentioned validity/expiration
boundaries").
"""

from pathlib import Path
import tempfile
import unittest

from server.importers.backfill import (
    DEFAULT_COMMITTED_REPORT_PATH,
    DEFAULT_DB_SNAPSHOT_PATH,
    DEFAULT_MARKDOWN_COMMITTED_REPORT_PATH,
    DEFAULT_REGISTRY_PATH,
    TEST_POLLUTION_NAME_PREFIXES,
    backfill_memory_fabric_57,
    reimport_default_db_86,
    reimport_default_db_episodes,
)
from server.journal.store import SqliteEventStore

skip_unless_real_state = unittest.skipUnless(
    DEFAULT_REGISTRY_PATH.exists() and DEFAULT_COMMITTED_REPORT_PATH.exists(),
    "Production registry/committed report not present on disk",
)
skip_unless_markdown_report = unittest.skipUnless(
    DEFAULT_MARKDOWN_COMMITTED_REPORT_PATH.exists(),
    "Markdown-summary committed report not present on disk",
)
skip_unless_snapshot = unittest.skipUnless(
    DEFAULT_DB_SNAPSHOT_PATH.exists(),
    "default_db pre-deletion snapshot not present on disk",
)


@skip_unless_real_state
class TestBackfillMemoryFabric57(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "journal.db"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_every_registry_record_resolves_to_a_source_event(self):
        with SqliteEventStore(self.db_path) as store:
            stats = backfill_memory_fabric_57(store)
            self.assertEqual(stats["registry_records"], 57)
            self.assertEqual(stats["events_journaled"], 57)
            self.assertEqual(store.stats()["total_events"], 57)

    def test_idempotent_rerun_produces_zero_new_events(self):
        with SqliteEventStore(self.db_path) as store:
            backfill_memory_fabric_57(store)
            second = backfill_memory_fabric_57(store)
            self.assertEqual(second["events_journaled"], 0)
            self.assertEqual(second["events_deduped"], 57)

    def test_events_are_marked_provenance_reconstructed(self):
        with SqliteEventStore(self.db_path) as store:
            backfill_memory_fabric_57(store)
            for event in store.query(harness="chatgpt"):
                if event.event_id.startswith("chatgpt:backfill:"):
                    self.assertTrue(event.metadata.get("provenance_reconstructed"))

    def test_retrospective_dates_preserved_through_backfill(self):
        with SqliteEventStore(self.db_path) as store:
            backfill_memory_fabric_57(store)

            gesture_event = store.get("chatgpt:backfill:cand_8f7ed87e0b4a")
            self.assertIsNotNone(gesture_event)
            self.assertEqual(gesture_event.event_date.year, 2014)

            text_analytics = store.get("chatgpt:backfill:cand_731a_episodic_text_analytics_commit")
            self.assertIsNotNone(text_analytics)
            self.assertEqual(text_analytics.event_date.date().isoformat(), "2023-12-19")

    def test_content_enriched_from_committed_report(self):
        with SqliteEventStore(self.db_path) as store:
            stats = backfill_memory_fabric_57(store)
            self.assertEqual(stats["matched_to_committed_report"], 57, "All 57 should match the committed report")
            event = store.get("chatgpt:backfill:cand_8f7ed87e0b4a")
            self.assertTrue(event.content.get("text"), "Backfilled event should carry real recovered text, not be empty")


@skip_unless_markdown_report
class TestReimportDefaultDb86(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "journal.db"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_validity_boundary_candidate_no_longer_misdated(self):
        """The specific defect this milestone fixed: cand_324's "valid
        through December 2026" must not produce a December 2026 event_date.
        """
        with SqliteEventStore(self.db_path) as store:
            reimport_default_db_86(store)
            events = store.query(harness="chatgpt")
            offending = [
                e
                for e in events
                if e.event_date and e.event_date.year == 2026 and e.event_date.month == 12
                and "valid through" in e.content.get("text", "").lower()
            ]
            self.assertEqual(offending, [], "No event should anchor to a mentioned validity/expiration boundary")

    def test_legitimate_future_scheduled_event_is_unaffected(self):
        """cand_354's "Owners Meeting / election: 2026-11-02" is a
        correctly-parsed future-scheduled event, not a regression — it
        must still carry its real date.
        """
        with SqliteEventStore(self.db_path) as store:
            reimport_default_db_86(store)
            events = store.query(harness="chatgpt")
            matches = [e for e in events if "owners meeting" in e.content.get("text", "").lower()]
            self.assertTrue(matches, "Expected to find the owners meeting candidate")
            self.assertEqual(matches[0].event_date.date().isoformat(), "2026-11-02")

    def test_idempotent_rerun_produces_zero_new_events(self):
        with SqliteEventStore(self.db_path) as store:
            first = reimport_default_db_86(store)
            second = reimport_default_db_86(store)
            self.assertEqual(second["events_journaled"], 0)
            self.assertEqual(second["events_deduped"], first["events_journaled"])

    def test_only_episodic_category_candidates_are_journaled(self):
        with SqliteEventStore(self.db_path) as store:
            stats = reimport_default_db_86(store)
            self.assertLess(stats["episodic_candidates"], stats["candidates_seen"])
            self.assertEqual(stats["events_journaled"] + stats["events_deduped"], stats["episodic_candidates"])


@skip_unless_snapshot
class TestReimportDefaultDbEpisodesFromSnapshot(unittest.TestCase):
    """The ground-truth full recovery (see backfill.py module docstring's
    correction of the Milestone 0.5 record: default_db's 86 episodes were
    38 real + 48 test pollution from a third, previously-unidentified
    offender, not "86 real episodes" as originally characterized).
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "journal.db"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_recovers_exactly_the_real_non_pollution_episodes(self):
        with SqliteEventStore(self.db_path) as store:
            stats = reimport_default_db_episodes(store)
            self.assertEqual(stats["episodes_seen"], 86)
            self.assertEqual(stats["real_episodes"], 38)
            self.assertEqual(stats["test_pollution_skipped"], 48)
            self.assertEqual(stats["events_journaled"], 38)

    def test_idempotent_rerun_produces_zero_new_events(self):
        with SqliteEventStore(self.db_path) as store:
            reimport_default_db_episodes(store)
            second = reimport_default_db_episodes(store)
            self.assertEqual(second["events_journaled"], 0)
            self.assertEqual(second["events_deduped"], 38)

    def test_no_test_pollution_name_reaches_the_journal(self):
        with SqliteEventStore(self.db_path) as store:
            reimport_default_db_episodes(store)
            for event in store.query(harness="chatgpt"):
                name = event.metadata.get("original_name", "")
                for prefix in TEST_POLLUTION_NAME_PREFIXES:
                    self.assertFalse(
                        name.startswith(prefix),
                        f"Test-pollution episode '{name}' should never reach the journal",
                    )

    def test_validity_boundary_episode_no_longer_misdated(self):
        with SqliteEventStore(self.db_path) as store:
            reimport_default_db_episodes(store)
            for event in store.query(harness="chatgpt"):
                text = event.content.get("text", "").lower()
                if "valid through" in text and event.event_date:
                    self.assertFalse(
                        event.event_date.year == 2026 and event.event_date.month == 12,
                        "A validity/expiration boundary must not be treated as the event's own date",
                    )

    def test_reconciled_episodes_keep_their_recorded_date(self):
        with SqliteEventStore(self.db_path) as store:
            reimport_default_db_episodes(store)
            reconciled = [
                e for e in store.query(harness="chatgpt") if e.metadata.get("original_name", "").startswith("reconciled_")
            ]
            self.assertEqual(len(reconciled), 18)
            for event in reconciled:
                self.assertIsNotNone(event.event_date)
                self.assertFalse(event.metadata.get("redated_at_backfill"))


if __name__ == "__main__":
    unittest.main()
