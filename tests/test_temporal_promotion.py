"""Promotion dates and order, so Graphiti can supersede facts itself.

Graphiti sets `invalid_at` on an older fact when a newer episode
contradicts it, deciding "newer" from each episode's reference_time
(stored as `valid_at`). That only works when undated episodes carry their
conversation time instead of "now", and when the older episode is already
in the graph before the one that replaced it arrives.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from server.consolidation.promotion import (
    PromotionStore,
    _chronological_key,
    promote_reviewed,
)
from server.consolidation.store import ConsolidationStore
from server.core.models import DatePrecision, SourceEvent, SourceProvenance
from server.journal.identity import compute_content_hash
from server.journal.store import SqliteEventStore
from server.policies.protocols import ExtractionCategory, ReasoningEpisode
from server.policies.reasoning_episode import REASONING_POLICY_VERSION
from server.review import actions
from server.review.store import ReviewStore

NOV5 = datetime(2026, 11, 5, 15, 30, tzinfo=timezone.utc)
NOV6 = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
NOV7 = datetime(2026, 11, 7, 18, 45, tzinfo=timezone.utc)


def _event(event_id: str, observed_at: datetime) -> SourceEvent:
    content = {"text": f"turn {event_id}"}
    return SourceEvent(
        schema_version="1.0", event_id=event_id, event_type="turn.completed",
        source=SourceProvenance(harness="claude_code", conversation_id="c1", turn_id=event_id),
        observed_at=observed_at, content=content, content_hash=compute_content_hash(content),
    )


def _episode(evidence: str, statement: str, event_date: datetime | None) -> ReasoningEpisode:
    return ReasoningEpisode(
        category=ExtractionCategory.EPISODIC, reasoning_kind="decision", statement=statement,
        confidence=0.9, evidence_event_ids=[evidence], event_date=event_date,
        date_precision=DatePrecision.DAY if event_date else DatePrecision.NONE,
    )


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "journal.db"
        self.journal = SqliteEventStore(db_path=self.db)
        self.cons = ConsolidationStore(db_path=self.db)
        self.prom = PromotionStore(db_path=self.db)
        self.rev = ReviewStore(db_path=self.db)
        self.calls: list[dict] = []

    def tearDown(self):
        for s in (self.journal, self.cons, self.prom, self.rev):
            s.close()
        self._tmp.cleanup()

    async def remember(self, **kw):
        self.calls.append(kw)
        return {"ok": True}

    def _seed(self, mid: str, observed_at: datetime, event_date: datetime | None, statement: str) -> str:
        eid = f"ev-{mid}"
        self.journal.append(_event(eid, observed_at))
        self.cons.record_reasoning_episode(
            job_id=f"job:{mid}", memory_id=mid, episode=_episode(eid, statement, event_date),
            policy_name="reasoning-episode", policy_version=REASONING_POLICY_VERSION,
            approval_state="queued_for_review", supersedes=None,
        )
        return mid


class TestReferenceTime(Base):
    async def test_undated_episode_uses_its_conversation_time_not_now(self):
        mid = self._seed("r:undated", NOV5, None, "Chose FalkorDB.")
        await promote_reviewed(self.cons, self.journal, self.prom, self.remember, [mid],
                               dry_run=False, inter_call_delay=0)
        self.assertEqual(self.calls[0]["reference_time"], NOV5)

    async def test_event_date_still_wins_over_conversation_time(self):
        mid = self._seed("r:dated", NOV7, datetime(2026, 11, 1), "Chose FalkorDB.")
        await promote_reviewed(self.cons, self.journal, self.prom, self.remember, [mid],
                               dry_run=False, inter_call_delay=0)
        self.assertEqual(self.calls[0]["reference_time"], datetime(2026, 11, 1))


class TestChronologicalOrder(Base):
    async def test_promote_reviewed_goes_oldest_first_whatever_the_input_order(self):
        newer = self._seed("r:newer", NOV7, None, "Switched the journal to Postgres.")
        older = self._seed("r:older", NOV7, datetime(2026, 11, 5), "Chose SQLite for the journal.")  # naive, day-only
        middle = self._seed("r:middle", NOV6, None, "Added WAL mode to SQLite.")
        run = await promote_reviewed(self.cons, self.journal, self.prom, self.remember, [newer, middle, older],
                                     dry_run=False, inter_call_delay=0)
        self.assertEqual([p["memory_id"] for p in run["promoted"]], [older, middle, newer])

    async def test_promote_approved_limit_takes_the_oldest_not_the_first_reviewed(self):
        newer = self._seed("r:newer", NOV7, None, "Switched the journal to Postgres.")
        older = self._seed("r:older", NOV5, None, "Chose SQLite for the journal.")
        actions.approve_episode(self.rev, newer)  # reviewed first
        actions.approve_episode(self.rev, older)
        run = await actions.promote_approved(self.cons, self.journal, self.prom, self.rev, self.remember,
                                             dry_run=False, inter_call_delay=0, limit=1)
        self.assertEqual([p["memory_id"] for p in run["promoted"]], [older])

    def test_named_ids_come_back_oldest_first(self):
        """The review confirm promotes a named list one id at a time; it must
        not follow the run's label order."""
        newer = self._seed("r:newer", NOV7, None, "Switched the journal to Postgres.")
        older = self._seed("r:older", NOV5, None, "Chose SQLite for the journal.")
        self.assertEqual(actions.chronological(self.cons, self.journal, [newer, "r:unknown", older]),
                         [older, newer, "r:unknown"])

    def test_undated_sorts_last(self):
        self.assertLess(_chronological_key(NOV7), _chronological_key(None))
        self.assertLess(_chronological_key(datetime(2026, 11, 5)), _chronological_key(NOV6))


class TestReviewViewShowsTheSameDate(Base):
    def test_attach_valid_at_falls_back_to_conversation_time(self):
        from server.episode_proposals import attach_valid_at

        self._seed("r:undated", NOV5, None, "Chose FalkorDB.")
        self._seed("r:dated", NOV7, datetime(2026, 11, 1), "Chose FalkorDB.")
        for s in (self.journal, self.cons):
            s._conn.commit()
        items = attach_valid_at([{"memory_id": "r:undated"}, {"memory_id": "r:dated"}], db_path=self.db)
        by_id = {i["memory_id"]: i for i in items}
        self.assertTrue(by_id["r:undated"]["valid_at"].startswith("2026-11-05T15:30"))
        self.assertEqual(by_id["r:undated"]["date_precision"], "conversation")
        self.assertTrue(by_id["r:dated"]["valid_at"].startswith("2026-11-01"))


class TestSetEventDate(Base):
    def _row(self, mid):
        return self.cons._conn.execute(
            "SELECT event_date, date_precision FROM derived_memories WHERE memory_id = ?", (mid,)).fetchone()

    def test_defaults_to_the_conversation_time_and_keeps_the_memory_id(self):
        mid = self._seed("r:undated", NOV5, None, "Chose FalkorDB.")
        dry = actions.set_event_date(self.cons._conn, self.rev, mid, reason="date it")
        self.assertTrue(dry["dry_run"])
        self.assertIsNone(self._row(mid)["event_date"])

        out = actions.set_event_date(self.cons._conn, self.rev, mid, reason="date it", dry_run=False)
        self.assertEqual(out["new"]["date_precision"], "exact")
        row = self._row(mid)
        self.assertEqual(datetime.fromisoformat(row["event_date"]), NOV5)
        audit = self.rev.audit_for(mid)
        self.assertEqual(audit[-1]["action"], "set_event_date")
        self.assertIsNone(self.rev.get(mid))  # no review verdict written

    def test_explicit_day_date(self):
        mid = self._seed("r:x", NOV7, None, "Chose FalkorDB.")
        actions.set_event_date(self.cons._conn, self.rev, mid, reason="r", event_date="2026-11-01", dry_run=False)
        self.assertEqual(tuple(self._row(mid)), ("2026-11-01", "day"))

    def test_bad_date_and_bad_precision_are_refused(self):
        mid = self._seed("r:x", NOV7, None, "Chose FalkorDB.")
        self.assertIn("error", actions.set_event_date(self.cons._conn, self.rev, mid, reason="r", event_date="Nov 1"))
        self.assertIn("error", actions.set_event_date(self.cons._conn, self.rev, mid, reason="r",
                                                      date_precision="week", dry_run=False))
        self.assertIsNone(self._row(mid)["event_date"])

    async def test_refused_once_promoted(self):
        mid = self._seed("r:x", NOV5, None, "Chose FalkorDB.")
        await promote_reviewed(self.cons, self.journal, self.prom, self.remember, [mid],
                               dry_run=False, inter_call_delay=0)
        out = actions.set_event_date(self.cons._conn, self.rev, mid, reason="r", dry_run=False)
        self.assertIn("correct_memory", out["error"])


if __name__ == "__main__":
    unittest.main()
