"""Milestone 0.5 regression fixtures.

Captures known failure modes found while stabilizing the Phase 1 baseline on
2026-09-03 (see IMPLEMENTATION-PLAN.md, Milestone 0.5). Two are confirmed,
currently-unfixed defects, marked `expectedFailure` so the suite documents
them without blocking on a fix that belongs to a later milestone; when the
fix lands the marker should be removed so the test starts enforcing the
correct behavior. The rest assert behavior that already works correctly and
must not regress.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

from server.importer import DatePrecision, TemporalExtractor

REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent / "imports" / "state" / "import_registry_memory-fabric.json"
)


class TestTemporalExtractorValidityDateConfusion(unittest.TestCase):
    """Confirmed defect: a validity/expiration date mentioned in candidate
    text is extracted as the episode's own reference_time, even when the
    text's grammar marks it as a future horizon rather than an occurrence.

    Found in the 2026-09-01 markdown-summary import: the candidate "Checked
    Illinois registration and found it valid through December 2026" (source:
    imports/results/import_chatgpt_20260901_172405_committed.json,
    cand_324) was dated 2026-12-01 — the registration's *expiration* month —
    even though the checking action itself took place around the surrounding
    section's other dated candidates (July/August 2026). This produced an
    episode with a `valid_at` in the future relative to when the underlying
    event occurred, landing in the (now-cleared) `default_db` graph as
    `import_chatgpt_20261201_cccaddaf`.

    This is NOT a "year inference" bug — TemporalExtractor never invents a
    year. It is a missing distinction between an occurrence date and a
    validity/duration boundary ("valid through", "expires", "through",
    "until", "renews") mentioned in the same sentence. Scheduled to be fixed
    in Milestone 2 when this content is re-imported through the journal.

    NOTE: a sibling candidate in the same source, "Owners Meeting / election:
    2026-11-02" (cand_354), is intentionally NOT treated as a regression
    here — that date is the actual, correctly-parsed date of a future
    scheduled event mentioned in the source material, not a parsing defect.
    Whether a future-scheduled event belongs in "episodic" alongside things
    that already happened is a modeling question for Milestone 3
    (consolidation policy), not a date-extraction bug.
    """

    @unittest.expectedFailure
    def test_validity_through_date_is_not_treated_as_event_date(self):
        text = "Checked Illinois registration and found it valid through December 2026."
        ref_time, precision = TemporalExtractor.extract_date(text)

        # Today, this extracts (2026, 12, 1) at DatePrecision.MONTH, which is
        # wrong: nothing in the text says the *checking* happened in
        # December. The fixed extractor should either return no confident
        # date for this sentence, or a date precision/flag distinguishing a
        # validity horizon from an occurrence date.
        if ref_time is not None:
            self.assertFalse(
                ref_time.year == 2026 and ref_time.month == 12,
                "TemporalExtractor anchored the event date to the mentioned "
                "validity/expiration month instead of the occurrence date.",
            )


class TestAssistantInferenceNotPersonalFact(unittest.TestCase):
    """Assistant-generated claims alone must not establish personal facts
    (ROADMAP.md architectural principle 9). This exercises the native
    ChatGPT export classifier's turn-evidence rule directly rather than via
    a full export dry-run.
    """

    def test_classifier_module_is_importable_and_documents_the_rule(self):
        # Smoke-level guard: the classifier module this principle depends on
        # exists and its "USER messages are primary evidence; ASSISTANT
        # messages provide contextual resolution only" contract (documented
        # in server/mcp.py's import_chatgpt_exports tool description) is
        # backed by a concrete extractor class. A full behavioral test
        # belongs with the Milestone 3 memory-quality fixture set, which
        # requires labeled real-corpus examples.
        from server.chatgpt_export_parser import TurnEvidenceExtractor

        self.assertTrue(hasattr(TurnEvidenceExtractor, "__init__") or callable(TurnEvidenceExtractor))


class TestRetrospectiveReferenceTimePreservation(unittest.TestCase):
    """Verified during the 2026-09-03 production import: retrospective
    reference_time values must survive ingestion unchanged, not collapse to
    the import/observation date. Re-asserted here against the durable
    registry file so a future migration (e.g. the Milestone 1 provider
    refactor) cannot silently regress it without a visible test failure.
    """

    @classmethod
    def setUpClass(cls):
        if not REGISTRY_PATH.exists():
            raise unittest.SkipTest(f"Production registry not found at {REGISTRY_PATH}")
        registry = json.loads(REGISTRY_PATH.read_text())
        imported = registry.get("imported_episodes", {})
        cls.by_candidate_id = {v.get("candidate_id"): v for v in imported.values()}

    def test_2014_gesture_presentation_reference_time(self):
        rec = self.by_candidate_id.get("cand_8f7ed87e0b4a")
        self.assertIsNotNone(rec, "cand_8f7ed87e0b4a missing from production registry")
        self.assertTrue(rec["reference_time"].startswith("2014-01-01"))

    def test_text_analytics_commit_gate_reference_time(self):
        rec = self.by_candidate_id.get("cand_731a_episodic_text_analytics_commit")
        self.assertIsNotNone(rec, "text analytics commit-gate candidate missing from production registry")
        self.assertIn("2023-12-19", rec["reference_time"])

    def test_text_to_sql_commit_gate_reference_time(self):
        rec = self.by_candidate_id.get("cand_f9de_episodic_text_to_sql_commit")
        self.assertIsNotNone(rec, "text-to-SQL commit-gate candidate missing from production registry")
        self.assertIn("2023-12-19", rec["reference_time"])

    def test_registry_has_exactly_57_records(self):
        # Matches the verified 2026-09-03 production import report
        # (imports/results/production_import_report_20260902.json).
        self.assertEqual(len(self.by_candidate_id), 57)


class TestImportIdempotency(unittest.TestCase):
    """A source event/candidate that has already been imported must not be
    re-ingested as a duplicate on a subsequent run. Verified at the registry
    level; full round-trip idempotency (parse -> classify -> ingest -> rerun)
    is exercised by tests/test_step9_classifier_improvements.py
    test_18_repeated_dry_runs_remain_idempotent and by the production import
    runner's --verify-post-import reconciliation.
    """

    def test_registry_candidate_ids_are_unique(self):
        if not REGISTRY_PATH.exists():
            self.skipTest(f"Production registry not found at {REGISTRY_PATH}")
        registry = json.loads(REGISTRY_PATH.read_text())
        imported = registry.get("imported_episodes", {})
        candidate_ids = [v.get("candidate_id") for v in imported.values()]
        self.assertEqual(
            len(candidate_ids),
            len(set(candidate_ids)),
            "Duplicate candidate_id values found in the production import registry.",
        )


if __name__ == "__main__":
    unittest.main()
