"""Milestone 3 consolidation pipeline tests.

Covers acceptance tests 1 (assistant statement guard), 3 (dry-run output
distinguishes additions/updates/rejections/ambiguities), and 4 (killing
the consolidator mid-job loses no captured event and leaves no partial
memory). Acceptance test 2 (replay under a new policy version creates a
new derivation with old lineage intact) is covered here too.
"""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from server.consolidation.pipeline import run_consolidation
from server.consolidation.store import ConsolidationStore
from server.core.models import DatePrecision, SourceEvent, SourceProvenance
from server.journal.store import SqliteEventStore
from server.policies.heuristic_v1 import HeuristicPatternPolicyV1
from server.policies.protocols import ExtractionCategory, ExtractionResult, PolicyContext


def make_event(event_id, text, actor_type="user", conversation_id="conv1", **overrides):
    from server.journal.identity import compute_content_hash

    content = {"text": text}
    defaults = dict(
        schema_version="1.0",
        event_id=event_id,
        event_type="turn.completed",
        source=SourceProvenance(harness="chatgpt", conversation_id=conversation_id, turn_id=event_id),
        actor_type=actor_type,
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        content=content,
        content_hash=compute_content_hash(content),
        date_precision=DatePrecision.NONE,
    )
    defaults.update(overrides)
    return SourceEvent(**defaults)


class TestAssistantStatementGuard(unittest.TestCase):
    """Milestone 3 acceptance test 1: a routine assistant statement with
    no user corroboration does not become a personal fact.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.journal_path = Path(self._tmpdir.name) / "journal.db"
        self.consolidation_path = Path(self._tmpdir.name) / "consolidation.db"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_assistant_only_conversation_produces_no_auto_accepted_memory(self):
        with SqliteEventStore(self.journal_path) as jstore:
            jstore.append(
                make_event(
                    "e1",
                    "On 2026-01-01, Todd decided to switch database providers to PostgreSQL.",
                    actor_type="assistant",
                )
            )

            with ConsolidationStore(self.consolidation_path) as cstore:
                stats = run_consolidation(jstore, cstore, HeuristicPatternPolicyV1())
                self.assertEqual(stats["by_category"].get("episodic", 0), 0)
                self.assertEqual(stats["by_approval_state"].get("auto_accepted", 0), 0)
                self.assertEqual(stats["by_category"].get("non_memory"), 1)

                rows = cstore.query_derived_memories()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["approval_state"], "rejected")


class TestDryRunOutputDistinguishesOutcomes(unittest.TestCase):
    """Milestone 3 acceptance test 3: dry-run output distinguishes
    additions (auto_accepted), updates (re-derivations), rejections
    (non_memory), and ambiguities (queued_for_review/ambiguous).

    The pipeline never writes to Graphiti at all (see module docstrings in
    server/consolidation/pipeline.py) — the consolidation store IS the
    "dry run" relative to episodic memory, so "dry-run output" here means
    "the consolidation store's queryable state after a run."
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.journal_path = Path(self._tmpdir.name) / "journal.db"
        self.consolidation_path = Path(self._tmpdir.name) / "consolidation.db"

        with SqliteEventStore(self.journal_path) as jstore:
            jstore.append(make_event("addition", "On 2026-03-15, completed the Q1 architecture review."))
            jstore.append(make_event("rejection", "Sounds good, glad that worked out!", actor_type="assistant"))
            jstore.append(make_event("ambiguous", "Thinking about switching database providers eventually."))

        with ConsolidationStore(self.consolidation_path) as cstore, SqliteEventStore(self.journal_path) as jstore:
            run_consolidation(jstore, cstore, HeuristicPatternPolicyV1())

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_additions_rejections_and_ambiguities_are_separately_queryable(self):
        with ConsolidationStore(self.consolidation_path) as cstore:
            auto_accepted = cstore.query_derived_memories(approval_state="auto_accepted")
            rejected = cstore.query_derived_memories(approval_state="rejected")
            queued = cstore.query_derived_memories(approval_state="queued_for_review")

            self.assertEqual({r["source_event_id"] for r in auto_accepted}, {"addition"})
            self.assertEqual({r["source_event_id"] for r in rejected}, {"rejection"})
            self.assertIn("ambiguous", {r["source_event_id"] for r in queued})


class TestCrashRecoveryLosesNothing(unittest.TestCase):
    """Milestone 3 acceptance test 4: killing the consolidator mid-job
    loses no captured event and leaves no partial memory.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.journal_path = Path(self._tmpdir.name) / "journal.db"
        self.consolidation_path = Path(self._tmpdir.name) / "consolidation.db"
        with SqliteEventStore(self.journal_path) as jstore:
            jstore.append(make_event("crash-event", "On 2026-02-02, renewed the annual insurance policy."))

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_simulated_crash_leaves_no_partial_derived_memory(self):
        policy = HeuristicPatternPolicyV1()
        memory_id = f"crash-event::{policy.name}@{policy.version}"
        job_id = f"job:{memory_id}"
        with ConsolidationStore(self.consolidation_path) as cstore:
            # Simulate a crash: mark the job running, then stop — exactly
            # what happens if the process dies between mark_running() and
            # record_consolidation() (see store.py's module docstring).
            cstore.mark_running(job_id, "crash-event", policy.name, policy.version)

            # No derived_memory should exist yet.
            self.assertIsNone(cstore.get_derived_memory(memory_id))
            job = cstore.get_job(job_id)
            self.assertEqual(job["status"], "running")

        # The source event itself is untouched regardless.
        with SqliteEventStore(self.journal_path) as jstore:
            event = jstore.get("crash-event")
            self.assertIsNotNone(event)
            self.assertIn("annual insurance policy", event.content["text"])

    def test_a_run_found_running_is_retried_not_skipped(self):
        policy = HeuristicPatternPolicyV1()
        memory_id = f"crash-event::{policy.name}@{policy.version}"
        job_id = f"job:{memory_id}"
        with ConsolidationStore(self.consolidation_path) as cstore:
            cstore.mark_running(job_id, "crash-event", policy.name, policy.version)

        with SqliteEventStore(self.journal_path) as jstore, ConsolidationStore(self.consolidation_path) as cstore:
            stats = run_consolidation(jstore, cstore, policy)
            self.assertEqual(stats["events_seen"], 1)
            self.assertEqual(stats["already_processed_skipped"], 0, "A 'running' job must be retried, not skipped")
            row = cstore.get_derived_memory(memory_id)
            self.assertIsNotNone(row)
            job = cstore.get_job(job_id)
            self.assertEqual(job["status"], "succeeded")


class TestReprocessingUnderNewPolicyVersion(unittest.TestCase):
    """Milestone 3 acceptance test 2 (adapted from "replay" to
    "reprocess", since this pipeline's replay is evidence re-emission —
    see server/journal/cli.py): reprocessing under a new policy version
    creates a new derivation with the prior version's lineage intact via
    `supersedes`, rather than overwriting it.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.journal_path = Path(self._tmpdir.name) / "journal.db"
        self.consolidation_path = Path(self._tmpdir.name) / "consolidation.db"
        with SqliteEventStore(self.journal_path) as jstore:
            jstore.append(make_event("versioned-event", "On 2026-04-04, migrated the database to PostgreSQL."))

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_new_policy_version_creates_a_linked_new_derivation(self):
        base_policy = HeuristicPatternPolicyV1()
        # Next-version stand-in, one version past whatever the real policy
        # currently is — not hardcoded to "1.1", since that now belongs to
        # the real HeuristicPatternPolicyV1 (bumped 2026-09-04; see that
        # class's docstring). Deriving it avoids this test silently
        # colliding with the real class's version again next time it bumps.
        next_version = f"{float(base_policy.version) + 0.1:.1f}"

        class NextVersionPolicy:
            name = base_policy.name
            version = next_version

            def __init__(self):
                self._base = HeuristicPatternPolicyV1()

            def evaluate(self, event, context):
                result = self._base.evaluate(event, context)
                # Trivial, clearly-different behavior to prove versioning
                # works — not a real policy improvement.
                return ExtractionResult(
                    category=result.category,
                    statement=result.statement,
                    reason=result.reason + f" (v{next_version} re-evaluation)",
                    confidence=result.confidence,
                    event_date=result.event_date,
                    date_precision=result.date_precision,
                )

        with SqliteEventStore(self.journal_path) as jstore, ConsolidationStore(self.consolidation_path) as cstore:
            run_consolidation(jstore, cstore, base_policy)
            v1_memory_id = f"versioned-event::{base_policy.name}@{base_policy.version}"
            v1_row = cstore.get_derived_memory(v1_memory_id)
            self.assertIsNotNone(v1_row)

            stats = run_consolidation(jstore, cstore, NextVersionPolicy())
            self.assertEqual(stats["re_derivations"], 1)

            v_next_memory_id = f"versioned-event::{base_policy.name}@{next_version}"
            v_next_row = cstore.get_derived_memory(v_next_memory_id)
            self.assertIsNotNone(v_next_row)
            self.assertEqual(v_next_row["supersedes"], v1_row["memory_id"])
            self.assertIn(f"v{next_version} re-evaluation", v_next_row["reason"])

            # The original derivation is untouched — lineage intact.
            v1_row_after = cstore.get_derived_memory(v1_memory_id)
            self.assertEqual(v1_row_after["reason"], v1_row["reason"])


if __name__ == "__main__":
    unittest.main()
