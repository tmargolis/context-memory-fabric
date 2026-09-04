"""Milestone 3 memory-quality evaluation.

Covers acceptance test 5 ("precision on the labelled fixture set meets a
threshold agreed at the exit gate") and grounds the exit gate's
auto-accept confidence threshold in a measured number rather than
intuition.

tests/fixtures/memory_quality/labeled_events.json is intentionally
referential only — event_id + expected label, no message content — for
two reasons: (1) the events it labels contain real personal content
(medical, legal, business matters) that must not be committed to the
repository, and (2) it lets the fixture be reviewed and versioned without
exposing what it's about. Its `event_id`s resolve only against Todd's own
production journal (imports/journal/journal.db, gitignored) — this whole
module skips cleanly when that journal isn't present (fresh clone, CI),
matching the precedent set by tests/test_ms2_importers.py's real-export
tests.

Label provenance: 35 "episodic, should become memory" labels come from
events whose original classification was independently verified during
Milestone 2's own work (the 57 backfilled memory-fabric episodes and the
38 default_db-recovered episodes were both already established as
genuinely episodic before this fixture existed — see
IMPLEMENTATION-PLAN.md's Milestone 2 section). 8 "non-memory" labels are
real assistant-authored Claude turns (architecturally guaranteed non-memory
by the actor-type guard, but worth confirming against real content, not
just a synthetic example). 8 more are real, manually-judged trivial user
turns ("set alarm for 6:45", "Used an Assistant feature") — routine
assistant-interaction chatter with no episodic or durable content.
"""

import json
from pathlib import Path
import unittest

from server.journal.store import DEFAULT_JOURNAL_PATH, SqliteEventStore
from server.policies.heuristic_v1 import HeuristicPatternPolicyV1
from server.policies.protocols import PolicyContext

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "memory_quality" / "labeled_events.json"

# Milestone 3 exit gate threshold — see server/consolidation/pipeline.py's
# DEFAULT_AUTO_ACCEPT_THRESHOLD, which this test's measured precision
# justifies.
AUTO_ACCEPT_THRESHOLD = 0.75


@unittest.skipUnless(DEFAULT_JOURNAL_PATH.exists(), f"Production journal not found at {DEFAULT_JOURNAL_PATH}")
class TestMemoryQualityPrecision(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE_PATH.read_text())
        cls.policy = HeuristicPatternPolicyV1()
        with SqliteEventStore() as store:
            cls.labeled_results = []
            for entry in cls.fixture:
                event = store.get(entry["event_id"])
                if event is None:
                    continue  # a labeled event missing from the journal; skip rather than fail the whole suite
                result = cls.policy.evaluate(event, PolicyContext())
                cls.labeled_results.append((entry, result))

    def test_fixture_loaded_and_resolves_against_the_journal(self):
        self.assertGreaterEqual(len(self.fixture), 40)
        self.assertGreaterEqual(len(self.labeled_results), 40, "Most labeled event_ids should resolve against the journal")

    def test_precision_on_auto_accepted_predictions(self):
        """Of everything the policy would auto-accept (category=episodic,
        confidence >= threshold), what fraction does the label set agree
        should actually become a memory? This is the number the exit
        gate's threshold is set from.
        """
        auto_accepted = [
            (entry, result)
            for entry, result in self.labeled_results
            if result.category.value == "episodic" and result.confidence >= AUTO_ACCEPT_THRESHOLD
        ]
        self.assertTrue(auto_accepted, "Expected at least some auto-accepted predictions in the fixture")
        correct = sum(1 for entry, _ in auto_accepted if entry["expected_should_become_memory"])
        precision = correct / len(auto_accepted)
        print(f"\nAuto-accept precision: {precision:.3f} ({correct}/{len(auto_accepted)}) at threshold {AUTO_ACCEPT_THRESHOLD}")
        self.assertGreaterEqual(precision, 0.9, "Auto-accept precision must stay high — false auto-accepts are the costly error")

    def test_non_memory_recall_on_known_negatives(self):
        """Of everything labeled non-memory, what fraction does the
        policy correctly keep out of auto-accepted/episodic? This is the
        guard's actual effectiveness on real content, not just the
        architectural guarantee for assistant-authored events.
        """
        negatives = [(entry, result) for entry, result in self.labeled_results if not entry["expected_should_become_memory"]]
        self.assertTrue(negatives)
        correctly_excluded = sum(
            1 for _, result in negatives if not (result.category.value == "episodic" and result.confidence >= AUTO_ACCEPT_THRESHOLD)
        )
        recall = correctly_excluded / len(negatives)
        print(f"Non-memory exclusion rate: {recall:.3f} ({correctly_excluded}/{len(negatives)})")
        self.assertEqual(recall, 1.0, "No known-negative should ever be auto-accepted")

    def test_assistant_authored_negatives_are_all_non_memory(self):
        assistant_entries = [
            (entry, result) for entry, result in self.labeled_results if entry.get("label_source") == "assistant_actor_type_guard"
        ]
        self.assertTrue(assistant_entries)
        for entry, result in assistant_entries:
            self.assertEqual(result.category.value, "non_memory")


if __name__ == "__main__":
    unittest.main()
