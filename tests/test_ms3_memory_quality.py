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

Label provenance, ORIGINAL (2026-09-03, 48 labels): 35 "episodic, should
become memory" labels came from events whose original classification was
independently verified during Milestone 2's own work (the 57 backfilled
memory-fabric episodes and the 38 default_db-recovered episodes). 8
"non-memory" labels were real assistant-authored Claude turns. 8 more were
real, manually-judged trivial user turns ("set alarm for 6:45", "Used an
Assistant feature").

**2026-09-04 correction**: at Todd's explicit direction, the 95
reconstructed/backfilled chatgpt events (see IMPLEMENTATION-PLAN.md's MS2
production-journal note) were deleted from the journal once the native
`conversations-*.json` export could finally be journaled directly — see
IMPLEMENTATION-PLAN.md's Milestone 3 exit gate for the full account. All 35
"episodic" labels above pointed at that deleted content and no longer
resolve; per Todd's decision, the fixture was shrunk to its 9 still-resolvable
rows rather than left silently broken or patched to hide the gap. **The
fixture currently contains zero positive ("should become memory") examples**
— `test_precision_on_auto_accepted_predictions` therefore *skips* rather
than measures anything until new positive labels exist, drawn from the
6,343 native chatgpt turns (or claude/gemini). The `AUTO_ACCEPT_THRESHOLD`
below remains at its originally-measured value (100% precision on 29/29 at
the time MS2/MS3 closed) — that historical measurement is preserved in
IMPLEMENTATION-PLAN.md's git history, but it is not independently
re-verifiable against the current journal without rebuilding the positive
side of this fixture.
"""

import json
from pathlib import Path
import unittest

from server.journal.store import DEFAULT_JOURNAL_PATH, SqliteEventStore
from server.policies.heuristic import HeuristicPatternPolicyV1
from server.policies.protocols import PolicyContext

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "memory_quality" / "labeled_events.json"

# Milestone 3 exit gate threshold — see server/consolidation/pipeline.py's
# DEFAULT_AUTO_ACCEPT_THRESHOLD, which this test's measured precision
# justifies.
AUTO_ACCEPT_THRESHOLD = 0.75


@unittest.skipUnless(
    DEFAULT_JOURNAL_PATH.exists() and FIXTURE_PATH.exists(),
    "Production journal and/or the (gitignored) memory-quality fixture not present",
)
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
        # Was >=40 against the original 48-label fixture; shrunk to 9 on
        # 2026-09-04 when its 39 chatgpt-backfill-sourced labels stopped
        # resolving (see module docstring's 2026-09-04 correction).
        self.assertGreaterEqual(len(self.fixture), 9)
        self.assertEqual(
            len(self.labeled_results), len(self.fixture), "Every remaining labeled event_id should resolve against the journal"
        )

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
        if not auto_accepted:
            # Honest skip, not a fabricated pass: as of the 2026-09-04
            # fixture correction (see module docstring), zero positive
            # ("should become memory") labels survive, so this metric is
            # currently unmeasurable rather than "measured and fine."
            self.skipTest(
                "No auto-accepted predictions in the current fixture — it has zero surviving positive labels "
                "since the 2026-09-04 correction. Auto-accept precision is not measurable until new positive "
                "labels (from native chatgpt/claude/gemini content) are added."
            )
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
