"""MS3.5 — reasoning-episode consolidation tests (ADR 0005).

Covers the six MS3.5 acceptance tests. The model call is faked throughout
(`FakeModel`), so nothing here needs GEMINI_API_KEY or a network; the rate
limiter is stubbed to always grant a fixed model.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from server.consolidation.pipeline import run_reasoning_consolidation
from server.consolidation.store import ConsolidationStore
from server.consolidation.threads import ThreadIndex
from server.consolidation.windowing import TimeGapWindower
from server.core.models import REASONING_KINDS, DatePrecision, SourceEvent, SourceProvenance
from server.journal.identity import compute_content_hash
from server.journal.store import SqliteEventStore
from server.policies.protocols import ExtractionCategory
from server.policies.reasoning_episode import ReasoningEpisodePolicyV1

BASE = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


def ev(event_id, text, actor_type="user", conversation_id="A", harness="chatgpt", minute_offset=0):
    content = {"text": text}
    return SourceEvent(
        schema_version="1.0",
        event_id=event_id,
        event_type="turn.completed",
        source=SourceProvenance(harness=harness, conversation_id=conversation_id, turn_id=event_id),
        actor_type=actor_type,
        observed_at=BASE + timedelta(minutes=minute_offset),
        content=content,
        content_hash=compute_content_hash(content),
        date_precision=DatePrecision.NONE,
    )


class _AlwaysGrant:
    """Stub GeminiRateLimiter — always grants, never counts."""

    def reserve(self, estimated_calls=None, now=None):
        return "fake-model"


class FakeModel:
    """Returns canned episode JSON based on markers in the prompt."""

    def __init__(self):
        self.calls = []

    def __call__(self, model: str, prompt: str) -> str:
        self.calls.append(prompt)
        if "LOGPASTE-MARKER" in prompt:
            return json.dumps({"episodes": []})
        if "DECISION-MARKER" in prompt:
            return json.dumps(
                {
                    "episodes": [
                        {
                            "reasoning_kind": "decision",
                            "statement": "Chose SQLite over Postgres for the journal.",
                            "rationale": "single-writer is fine at this volume",
                            "status": "resolved",
                            "thread_key": "journal-backend",
                            "confidence": 0.9,
                            "turn_numbers": [1],
                        },
                        {
                            "reasoning_kind": "rejected_alternative",
                            "statement": "Postgres was considered and set aside.",
                            "rationale": "operational weight not justified yet",
                            "thread_key": "journal-backend",
                            "confidence": 0.8,
                            "turn_numbers": [1],
                        },
                    ]
                }
            )
        if "THREAD-A-MARKER" in prompt:
            return json.dumps(
                {
                    "episodes": [
                        {
                            "reasoning_kind": "plan",
                            "statement": "Intends to move OpenClaw gateway onto the Mac Pro.",
                            "status": "open",
                            "thread_key": "openclaw-gateway-move",
                            "confidence": 0.7,
                            "turn_numbers": [1],
                        }
                    ]
                }
            )
        if "THREAD-B-MARKER" in prompt:
            return json.dumps(
                {
                    "episodes": [
                        {
                            "reasoning_kind": "finding",
                            "statement": "OpenClaw gateway move completed; connection stable.",
                            "status": "resolved",
                            "thread_key": "OpenClaw Gateway Move",  # different surface form, same thread
                            "confidence": 0.85,
                            "turn_numbers": [1],
                        }
                    ]
                }
            )
        # default: a single investigation with no date and no closure verb
        return json.dumps(
            {
                "episodes": [
                    {
                        "reasoning_kind": "investigation",
                        "statement": "Working through whether the thread key should be a string or an embedding.",
                        "driving_question": "string vs embedding for the thread key?",
                        "status": "open",
                        "thread_key": "thread-key-representation",
                        "confidence": 0.66,
                        "turn_numbers": [1, 3, 5],
                    }
                ]
            }
        )


class MS35Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.journal_path = Path(self._tmp.name) / "journal.db"
        self.cons_path = Path(self._tmp.name) / "consolidation.db"
        self.model = FakeModel()

    def tearDown(self):
        self._tmp.cleanup()

    def policy(self):
        return ReasoningEpisodePolicyV1(generate_fn=self.model, rate_limiter=_AlwaysGrant())

    def _consolidate(self, **kw):
        with SqliteEventStore(self.journal_path) as j, ConsolidationStore(self.cons_path) as c:
            ti = ThreadIndex(self.cons_path)
            try:
                return run_reasoning_consolidation(j, c, self.policy(), thread_index=ti, **kw)
            finally:
                ti.close()

    def _append(self, events):
        with SqliteEventStore(self.journal_path) as j:
            for e in events:
                j.append(e)


class TestUndatedInvestigation(MS35Base):
    """Acceptance 1: an investigation spanning ~10 turns with no closure verb
    and no in-text date -> one episodic memory, reasoning_kind='investigation',
    event_date resolved from window turn metadata.
    """

    def test_investigation_episode_dated_from_metadata(self):
        events = []
        for i in range(10):
            actor = "user" if i % 2 == 0 else "assistant"
            events.append(ev(f"e{i}", f"turn {i} about the design, still not sure? {i}", actor, minute_offset=i))
        self._append(events)

        stats = self._consolidate(triage=True)
        self.assertEqual(stats["episodes_created"], 1)
        self.assertEqual(stats["by_reasoning_kind"].get("investigation"), 1)

        with ConsolidationStore(self.cons_path) as c:
            rows = c.query_derived_memories()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["category"], ExtractionCategory.EPISODIC.value)
        self.assertEqual(row["reasoning_kind"], "investigation")
        self.assertEqual(row["approval_state"], "queued_for_review")  # threshold=None default
        self.assertIsNotNone(row["event_date"])
        # date came from a turn, not from text — within the window's span
        self.assertTrue(row["event_date"].startswith("2026-06-01"))
        self.assertEqual(row["date_precision"], DatePrecision.DAY.value)
        self.assertEqual(json.loads(row["evidence_event_ids_json"]), ["e0", "e2", "e4"])


class TestDecisionAndRejectedAlternative(MS35Base):
    """Acceptance 2: a "considered X, chose Y because Z" window produces both
    a `decision` episode and a linked `rejected_alternative` episode.
    """

    def test_decision_plus_rejected_alternative(self):
        self._append(
            [
                ev("d1", "DECISION-MARKER considered Postgres, choosing SQLite because single-writer is fine? yes", "user", minute_offset=0),
                ev("d2", "Sounds reasonable given the volume.", "assistant", minute_offset=1),
                ev("d3", "right, locking that in", "user", minute_offset=2),
            ]
        )
        stats = self._consolidate(triage=True)
        self.assertEqual(stats["episodes_created"], 2)
        kinds = stats["by_reasoning_kind"]
        self.assertEqual(kinds.get("decision"), 1)
        self.assertEqual(kinds.get("rejected_alternative"), 1)

        with ThreadIndex(self.cons_path) as ti:
            thread = ti.get("journal-backend")
        self.assertIsNotNone(thread)
        self.assertEqual(thread.episode_count, 2)
        self.assertCountEqual(thread.reasoning_kinds, ["decision", "rejected_alternative"])


class TestShortLogPasteNeverRawEpisodic(MS35Base):
    """Acceptance 3: a short log-narration turn is either a legitimate
    reasoning synthesis or excluded — never a raw-paste episodic promotion.
    Here triage withholds it and nothing is written as a memory.
    """

    def test_short_logpaste_is_triaged_out_not_promoted(self):
        self._append(
            [
                ev("l1", "watcher started\nwatcher stopped\nwatcher started", "user", minute_offset=0),
                ev("l2", "ok", "user", minute_offset=1),
                ev("l3", "hmm", "user", minute_offset=2),
            ]
        )
        stats = self._consolidate(triage=True)
        self.assertEqual(stats["windows_triaged_out"], 1)
        self.assertEqual(stats["episodes_created"], 0)
        with ConsolidationStore(self.cons_path) as c:
            self.assertEqual(c.query_derived_memories(), [])
            job = c.get_job(list(c._conn.execute("SELECT job_id FROM consolidation_jobs"))[0][0])
        self.assertEqual(job["status"], "triaged_out")

    def test_model_returning_no_episodes_writes_no_memory(self):
        self._append(
            [
                ev("m1", "LOGPASTE-MARKER here is a big log, why is it looping? trace follows", "user", minute_offset=0),
                ev("m2", "Looks like a restart loop.", "assistant", minute_offset=1),
                ev("m3", "still looping, what causes that", "user", minute_offset=2),
            ]
        )
        stats = self._consolidate(triage=True)
        self.assertEqual(stats["windows_sent_to_model"], 1)
        self.assertEqual(stats["episodes_created"], 0)
        with ConsolidationStore(self.cons_path) as c:
            self.assertEqual(c.query_derived_memories(), [])

    def test_single_exchange_window_is_below_the_reasoning_floor(self):
        """min_window_events floor (2026-09-06): a 2-event one-shot
        exchange is withheld even with a question mark — no working-through.
        """
        self._append(
            [
                ev("s1", "how do I make a support call from an alternate phone?", "user", minute_offset=0),
                ev("s2", "You can dial ...", "assistant", minute_offset=1),
            ]
        )
        stats = self._consolidate(triage=True)
        self.assertEqual(stats["windows_triaged_out"], 1)
        self.assertEqual(stats["windows_sent_to_model"], 0)
        with ConsolidationStore(self.cons_path) as c:
            job = c.get_job(list(c._conn.execute("SELECT job_id FROM consolidation_jobs"))[0][0])
        self.assertEqual(job["status"], "triaged_out")
        self.assertIn("below reasoning floor", job["last_error"])

    def test_floor_can_be_disabled(self):
        self._append(
            [
                ev("d1", "still not sure which approach? weighing it", "user", minute_offset=0),
                ev("d2", "both work", "assistant", minute_offset=1),
            ]
        )
        stats = self._consolidate(triage=True, min_window_events=1)
        self.assertEqual(stats["windows_sent_to_model"], 1)


class TestCrossConversationThread(MS35Base):
    """Acceptance 4: an intent stated in conversation A and its outcome
    reported in conversation B (different harness) land in the same thread.
    """

    def test_intent_and_outcome_join_one_thread(self):
        self._append(
            [
                ev("a1", "THREAD-A-MARKER planning to move the gateway? yes", "user", conversation_id="A", harness="chatgpt", minute_offset=0),
                ev("a2", "makes sense", "assistant", conversation_id="A", harness="chatgpt", minute_offset=1),
                ev("a3", "will do it this week", "user", conversation_id="A", harness="chatgpt", minute_offset=2),
                # days later, different conversation, different assistant
                ev("b1", "THREAD-B-MARKER did the gateway move, is it stable? seems so", "user", conversation_id="B", harness="gemini", minute_offset=6000),
                ev("b2", "Stable now.", "assistant", conversation_id="B", harness="gemini", minute_offset=6001),
                ev("b3", "great, done", "user", conversation_id="B", harness="gemini", minute_offset=6002),
            ]
        )
        stats = self._consolidate(triage=True)
        self.assertEqual(stats["episodes_created"], 2)

        with ThreadIndex(self.cons_path) as ti:
            thread = ti.get("openclaw-gateway-move")
            index_stats = ti.stats()
        self.assertIsNotNone(thread)
        self.assertEqual(index_stats["total_threads"], 1)  # both surface forms collapsed to one
        self.assertTrue(thread.is_cross_conversation)
        self.assertTrue(thread.is_cross_harness)
        self.assertCountEqual(thread.harnesses, ["chatgpt", "gemini"])
        self.assertCountEqual(thread.conversation_ids, ["A", "B"])
        self.assertEqual(thread.episode_count, 2)
        self.assertEqual(thread.status, "resolved")  # the conv-B finding closed it


class TestReprocessUnderBumpedVersion(MS35Base):
    """Acceptance 5: reprocessing under a bumped policy version creates new
    derivations with prior lineage intact.
    """

    def test_bumped_version_supersedes_prior(self):
        self._append(
            [
                ev("r0", "still working out the thread key design? yes", "user", minute_offset=0),
                ev("r1", "one option is embeddings", "assistant", minute_offset=1),
                ev("r2", "or just a normalized string, not sure which", "user", minute_offset=2),
                ev("r3", "both have tradeoffs", "assistant", minute_offset=3),
                ev("r4", "leaning string for now", "user", minute_offset=4),
            ]
        )
        base_version = ReasoningEpisodePolicyV1.version
        first = self._consolidate(triage=True)
        self.assertEqual(first["episodes_created"], 1)

        # bump version and re-run
        with SqliteEventStore(self.journal_path) as j, ConsolidationStore(self.cons_path) as c:
            pol = ReasoningEpisodePolicyV1(generate_fn=self.model, rate_limiter=_AlwaysGrant())
            pol.version = "0.9-test"
            ti = ThreadIndex(self.cons_path)
            second = run_reasoning_consolidation(j, c, pol, thread_index=ti, triage=True)
            ti.close()
            rows = c.query_derived_memories()

        self.assertEqual(second["episodes_created"], 1)
        by_version = {r["policy_version"]: r for r in rows}
        self.assertEqual(set(by_version), {base_version, "0.9-test"})
        self.assertIsNone(by_version[base_version]["supersedes"])
        self.assertEqual(by_version["0.9-test"]["supersedes"], by_version[base_version]["memory_id"])


class TestNoNewCategory(unittest.TestCase):
    """Acceptance 6: no new ExtractionCategory value exists."""

    def test_extraction_category_still_four_values(self):
        self.assertEqual(
            sorted(c.value for c in ExtractionCategory),
            ["ambiguous", "durable_candidate", "episodic", "non_memory"],
        )

    def test_reasoning_kinds_are_not_categories(self):
        for kind in REASONING_KINDS:
            self.assertNotIn(kind, {c.value for c in ExtractionCategory})


class TestTriageOptional(MS35Base):
    """The triage gate is optional (ADR 0005 decision 2 / 2026-09-05)."""

    def test_triage_off_sends_everything(self):
        self._append(
            [
                ev("t1", "watcher started\nwatcher stopped", "user", minute_offset=0),
                ev("t2", "k", "user", minute_offset=1),
                ev("t3", "ok", "user", minute_offset=2),
            ]
        )
        stats = self._consolidate(triage=False)
        self.assertEqual(stats["windows_triaged_out"], 0)
        self.assertEqual(stats["windows_sent_to_model"], 1)


class TestPhaseDFixtureConfidenceDoesNotSeparate(unittest.TestCase):
    """MS3.5 exit gate (2026-09-07): the Phase D fixture exists to answer
    'what is the reasoning-episode auto-accept threshold' — and the answer is
    'there isn't one'. Model confidence does not separate the human keep/drop
    calls, so `_reasoning_approval_state` stays at threshold=None. This guards
    the finding: if a future policy version makes confidence meaningful, this
    test fails loudly and the exit gate gets revisited.
    """

    FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/memory_quality/reasoning_labels.json"

    def test_no_confidence_threshold_beats_baseline_by_much(self):
        if not self.FIXTURE.exists():
            self.skipTest("reasoning_labels.json not present (Phase D fixture)")
        rows = json.loads(self.FIXTURE.read_text())
        keep = [r for r in rows if r["verdict"] == "good" and r.get("confidence") is not None]
        drop = [r for r in rows if r["verdict"] == "bad" and r.get("confidence") is not None]
        if len(keep) < 5 or len(drop) < 5:
            self.skipTest("fixture too small / one-sided to evaluate separation")

        total = len(keep) + len(drop)
        baseline = max(len(keep), len(drop)) / total  # always-predict-majority
        best = 0.0
        for t in sorted({r["confidence"] for r in rows if r.get("confidence") is not None}):
            acc_high = (sum(c["confidence"] >= t for c in keep) + sum(d["confidence"] < t for d in drop)) / total
            acc_low = (sum(c["confidence"] < t for c in keep) + sum(d["confidence"] >= t for d in drop)) / total
            best = max(best, acc_high, acc_low)
        # If confidence ever separates keep/drop well (> baseline + 0.20), the
        # "no auto-accept threshold" exit-gate answer no longer holds.
        self.assertLess(
            best, baseline + 0.20,
            f"confidence now separates keep/drop (best {best:.0%} vs baseline {baseline:.0%}) — revisit the MS3.5 exit gate",
        )


class TestMaxWindowsBound(MS35Base):
    """`max_windows` bounds a probe run without marking unreached windows."""

    def test_stops_at_max_windows(self):
        events = []
        for conv in range(4):
            for i in range(4):
                actor = "user" if i % 2 == 0 else "assistant"
                events.append(
                    ev(f"c{conv}e{i}", f"design question {i} for topic {conv}? yes", actor, conversation_id=f"C{conv}", minute_offset=i)
                )
        self._append(events)
        stats = self._consolidate(triage=True, max_windows=2)
        self.assertTrue(stats["stopped_at_max_windows"])
        self.assertEqual(stats["windows_sent_to_model"], 2)


if __name__ == "__main__":
    unittest.main()
