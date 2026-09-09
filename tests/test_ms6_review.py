"""MS6 — review, audit and bulk governance.

remember() is faked; no Gemini/FalkorDB. Tests the review bookkeeping:
the project taxonomy, the queue's grouping, the single-chokepoint audit
trail, bulk actions and their reversibility, and evidence resolution.

Acceptance test 7 is deliberately NOT the plan's "decisions-made vs
episodes-reviewed" — measured against the production journal, by-thread
review clusters 315 tier-1 episodes into 215 threads (1.47x, 73%
singletons), so that assertion passes on a technicality while saving a
reviewer nothing. What the review pass actually costs is *re-orientation*,
so the test asserts the grouping property that reduces it: buckets are
few, and no bucket is a singleton pile.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from server.consolidation.promotion import PromotionStore
from server.consolidation.store import ConsolidationStore
from server.core.models import DatePrecision, SourceEvent, SourceProvenance
from server.journal.identity import compute_content_hash
from server.journal.store import SqliteEventStore
from server.policies.reasoning_episode_v1 import REASONING_POLICY_VERSION
from server.policies.protocols import ExtractionCategory, ExtractionResult, ReasoningEpisode
from server.review import actions, projects
from server.review.explain import explain, parse_reason, resolve_evidence
from server.review.queue import review_queue
from server.review.store import APPROVED, DEFERRED, PENDING, REJECTED, ReviewStore

BASE = datetime(2026, 5, 1, tzinfo=timezone.utc)

# Tests pin their own taxonomy. The real one is personal and gitignored, so a
# test that read it would pass only on one machine and fail in CI.
TAXONOMY = Path(__file__).parent / "fixtures" / "review" / "taxonomy.test.json"


def ev(event_id, text="working through the backend choice", actor="user", harness="claude"):
    content = {"text": text}
    return SourceEvent(
        schema_version="1.0",
        event_id=event_id,
        event_type="turn.completed",
        source=SourceProvenance(harness=harness, conversation_id="c1", turn_id=event_id),
        observed_at=BASE,
        content=content,
        content_hash=compute_content_hash(content),
        actor_type=actor,
    )


def episode(kind="decision", evidence=("e0",), statement="Chose SQLite for the journal.", thread="cmf-journal-backend"):
    return ReasoningEpisode(
        category=ExtractionCategory.EPISODIC,
        reasoning_kind=kind,
        statement=statement,
        confidence=0.9,
        evidence_event_ids=list(evidence),
        event_date=BASE,
        date_precision=DatePrecision.DAY,
        thread_key=thread,
        rationale="the journal must survive a crash mid-write",
        driving_question="which store backs the journal?",
        status="resolved",
    )


class MS6Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "journal.db"
        self.journal = SqliteEventStore(db_path=self.db)
        self.cons = ConsolidationStore(db_path=self.db)
        self.prom = PromotionStore(db_path=self.db)
        self.rev = ReviewStore(db_path=self.db)
        self.calls = []

    def tearDown(self):
        for s in (self.journal, self.cons, self.prom, self.rev):
            s.close()
        self._tmp.cleanup()

    async def ok_remember(self, **kw):
        self.calls.append(kw)
        return {"ok": True}

    def _reason_row(self, mid, epi):
        self.cons.record_reasoning_episode(
            job_id=f"job:{mid}", memory_id=mid, episode=epi,
            policy_name="reasoning-episode", policy_version=REASONING_POLICY_VERSION,
            approval_state="queued_for_review", supersedes=None,
        )

    def _heuristic_row(
        self, mid, source_event_id, state="queued_for_review",
        category=ExtractionCategory.AMBIGUOUS, event_date=None,
    ):
        self.cons.record_consolidation(
            job_id=f"job:{mid}", memory_id=mid, source_event_id=source_event_id,
            policy_name="heuristic-pattern", policy_version="1.2",
            result=ExtractionResult(
                category=category, statement="how many steps in 1.4 MI", reason="x", confidence=0.2,
                event_date=event_date,
                date_precision=DatePrecision.DAY if event_date else DatePrecision.NONE,
            ),
            approval_state=state, supersedes=None,
        )

    def _seed(self, n_tier1=3, project_thread="cmf-journal-backend"):
        ids = []
        for i in range(n_tier1):
            self.journal.append(ev(f"e{i}"))
            mid = f"r:{i}::reasoning-episode@0.2"
            self._reason_row(mid, episode(evidence=[f"e{i}"], thread=project_thread))
            ids.append(mid)
        projects.backfill(self.cons._conn, dry_run=False, taxonomy=TAXONOMY)
        return ids


# ---------------------------------------------------------------------------
# Project taxonomy — the review unit
# ---------------------------------------------------------------------------
class TestProjects(unittest.TestCase):
    def setUp(self):
        self.rules = projects.load_rules(TAXONOMY)

    def classify(self, slug, statement=None):
        # `overrides` must be pinned too — `classify()` defaults it to the
        # REAL taxonomy.local.json's overrides when omitted, same as it
        # defaults `compiled` to the real rules. Passing `self.rules` but
        # leaving overrides implicit made this fixture-based test silently
        # pick up production overrides (caught when adding the
        # career-navigator-dev split: this test started reading the real
        # taxonomy.local.json instead of the fixture).
        return projects.classify(slug, statement, self.rules, {})

    def test_first_match_wins_ordering(self):
        # career-navigator must beat the bare `claude` rule
        self.assertEqual(self.classify("career-navigator-plugin-design"), "career-navigator")
        # agent-fabric is the old name for interlock; openclaw stays separate
        self.assertEqual(self.classify("agent-fabric-ip-protection"), "interlock")
        self.assertEqual(self.classify("openclaw-gateway-connection"), "openclaw")
        # AstroAlert is app development, not the imaging hobby
        self.assertEqual(self.classify("astroalert-notification-scheduling"), "android-apps")
        self.assertEqual(self.classify("astrophotography-calibration-workflow"), "astrophotography")

    def test_word_boundaries_do_not_over_match(self):
        # "art" must not fire on "smart"; \b is what prevents it
        self.assertNotEqual(self.classify("smart-home-thermostat"), "art-exhibition")

    def test_unknown_falls_back_to_misc_not_none(self):
        self.assertEqual(self.classify("zzz-unmatched-topic", "nothing relevant here"), projects.MISC)

    def test_statement_is_used_only_when_there_is_no_slug(self):
        self.assertEqual(self.classify(None, "The user decided to reconfigure the Synology NAS"), "mac-infra")

    def test_statement_never_overrides_a_slug_that_matches_nothing(self):
        """Prose mentions words in passing; the slug is the model's own label.

        Matching the statement as a fallback put `condo-art-lighting` in
        context-memory-fabric on the word "wiki" and `neurologist-follow-up-prep`
        in mac-infra on "recovery". A slug that matches no rule means `misc`,
        which is reviewed like any other bucket — a confidently wrong bucket
        is the expensive outcome, not an honest unknown.
        """
        self.assertEqual(
            self.classify("neurologist-follow-up-prep", "planning data recovery after the appointment"),
            projects.MISC,
        )

    def test_interlock_wins_over_openclaw_when_both_appear(self):
        # OpenClaw is its own project except when the subject is testing
        # Interlock on it, so the interlock rule is ordered first.
        self.assertEqual(self.classify("openclaw-interlock-integration-test"), "interlock")
        self.assertEqual(self.classify("openclaw-gateway-connection"), "openclaw")

    def test_override_beats_every_rule(self):
        """A reviewer's exact-match correction outranks the heuristics.

        Corrections are frequently not patterns —
        "tartan-weaving-mill-order-delay belongs in writing" is a judgement
        about one thread. Forcing it into a regex would encode a word that
        misfires elsewhere; an exact-match override cannot misfire.
        """
        rules = projects.load_rules(TAXONOMY)
        self.assertEqual(projects.classify("openclaw-gateway-connection", None, rules), "openclaw")
        self.assertEqual(
            projects.classify("openclaw-gateway-connection", None, rules, {"openclaw-gateway-connection": "interlock"}),
            "interlock",
        )

    def test_override_only_matches_the_exact_slug(self):
        rules = projects.load_rules(TAXONOMY)
        ov = {"condo-board-letter": "writing"}
        self.assertEqual(projects.classify("condo-board-letter", None, rules, ov), "writing")
        self.assertEqual(projects.classify("condo-board-letter-v2", None, rules, ov), "condo")

    def test_no_taxonomy_file_classifies_everything_misc(self):
        # Deliberate: a shipped default would be someone else's project list
        # quietly mislabelling this corpus. An honest `misc` is reviewable.
        self.assertEqual(projects.classify("openclaw-gateway", None, []), projects.MISC)

    def test_missing_taxonomy_path_is_reported_clearly(self):
        with self.assertRaises(ValueError) as ctx:
            projects.load_rules(Path("/nonexistent/taxonomy.json"))
        self.assertIn("could not read taxonomy", str(ctx.exception))

    def test_parse_thread_key_from_flattened_reason(self):
        self.assertEqual(
            projects.parse_thread_key("reasoning_kind=decision | why: x | status=open | thread=openclaw-gateway"),
            "openclaw-gateway",
        )
        self.assertIsNone(projects.parse_thread_key("reasoning_kind=decision | why: x"))


class TestBackfill(MS6Base):
    def test_populates_and_is_idempotent(self):
        self._seed(2)
        rows = self.cons._conn.execute(
            "SELECT thread_key, project FROM derived_memories WHERE policy_name='reasoning-episode'"
        ).fetchall()
        self.assertTrue(all(r["thread_key"] == "cmf-journal-backend" for r in rows))
        self.assertTrue(all(r["project"] == "context-memory-fabric" for r in rows))
        # a second run must converge to zero, including for rows with no thread
        self.assertEqual(projects.backfill(self.cons._conn, dry_run=False, taxonomy=TAXONOMY)["rows_updated"], 0)

    def test_backfill_defaults_to_dry_run(self):
        # Consistency with every other mutating helper: writing is opt-in.
        self.journal.append(ev("e0"))
        self._reason_row("r:0::reasoning-episode@0.2", episode(evidence=["e0"]))
        result = projects.backfill(self.cons._conn, taxonomy=TAXONOMY)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["rows_updated"], 0)
        row = self.cons._conn.execute(
            "SELECT project FROM derived_memories WHERE memory_id = 'r:0::reasoning-episode@0.2'"
        ).fetchone()
        self.assertIsNone(row["project"])

    def test_row_without_thread_key_still_converges(self):
        self.journal.append(ev("e9"))
        self._reason_row("r:9::reasoning-episode@0.2", episode(evidence=["e9"], thread=None))
        projects.backfill(self.cons._conn, dry_run=False, taxonomy=TAXONOMY)
        self.assertEqual(projects.backfill(self.cons._conn, dry_run=False, taxonomy=TAXONOMY)["rows_updated"], 0)


# ---------------------------------------------------------------------------
# Acceptance 1 — explain() answers "why does this memory exist"
# ---------------------------------------------------------------------------
class TestExplain(MS6Base):
    def test_returns_statement_evidence_turns_and_thread(self):
        self.journal.append(ev("e0", text="I think SQLite is the right call here"))
        self.journal.append(ev("e1", text="agreed, one file keeps it consistent", actor="assistant"))
        self._reason_row("r:0::reasoning-episode@0.2", episode(evidence=["e0", "e1"]))
        projects.backfill(self.cons._conn, dry_run=False, taxonomy=TAXONOMY)

        result = explain(self.cons._conn, "r:0::reasoning-episode@0.2")
        self.assertEqual(result["statement"], "Chose SQLite for the journal.")
        self.assertEqual(result["reasoning_kind"], "decision")
        self.assertEqual(result["thread_key"], "cmf-journal-backend")
        self.assertEqual(result["project"], "context-memory-fabric")
        self.assertEqual(result["rationale"], "the journal must survive a crash mid-write")
        self.assertEqual(len(result["evidence"]), 2)
        self.assertEqual(result["evidence"][0]["text"], "I think SQLite is the right call here")
        self.assertEqual(result["evidence"][1]["actor"], "assistant")

    def test_missing_memory_returns_none(self):
        self.assertIsNone(explain(self.cons._conn, "nope"))

    def test_evidence_preserves_citation_order_and_flags_pruned_turns(self):
        self.journal.append(ev("e1"))
        turns = resolve_evidence(self.cons._conn, ["e1", "gone"])
        self.assertEqual([t["event_id"] for t in turns], ["e1", "gone"])
        self.assertTrue(turns[1]["missing"])

    def test_evidence_truncates_long_turns(self):
        self.journal.append(ev("e0", text="x" * 5000))
        turns = resolve_evidence(self.cons._conn, ["e0"], max_chars=100)
        self.assertEqual(len(turns[0]["text"]), 100)
        self.assertTrue(turns[0]["truncated"])

    def test_parse_reason_degrades_to_raw_on_unknown_shape(self):
        self.assertEqual(parse_reason("free text with no fields"), {"raw": "free text with no fields"})
        self.assertEqual(parse_reason(None), {})


# ---------------------------------------------------------------------------
# Acceptance 7 (rewritten) — the grouping that reduces re-orientation
# ---------------------------------------------------------------------------
class TestQueueGrouping(MS6Base):
    def test_groups_by_project_not_thread(self):
        # Three distinct thread slugs that are one project — exactly the case
        # by-thread review gets wrong (it would show three separate entries).
        for i, thread in enumerate(
            ["openclaw-gateway-connection", "openclaw-gateway-setup", "openclaw-agent-routing"]
        ):
            self.journal.append(ev(f"e{i}"))
            self._reason_row(f"r:{i}::reasoning-episode@0.2", episode(evidence=[f"e{i}"], thread=thread))
        projects.backfill(self.cons._conn, dry_run=False, taxonomy=TAXONOMY)

        q = review_queue(self.cons._conn, self.rev, self.prom, tier=1)
        self.assertEqual(q["bucket_count"], 1)
        self.assertEqual(q["buckets"][0]["project"], "openclaw")
        self.assertEqual(len(q["buckets"][0]["episodes"]), 3)
        self.assertEqual(len({e["thread_key"] for e in q["buckets"][0]["episodes"]}), 3)

    def test_buckets_ordered_by_tier1_density(self):
        for i, thread in enumerate(["openclaw-a", "openclaw-b", "condo-board-letter"]):
            self.journal.append(ev(f"e{i}"))
            self._reason_row(f"r:{i}::reasoning-episode@0.2", episode(evidence=[f"e{i}"], thread=thread))
        projects.backfill(self.cons._conn, dry_run=False, taxonomy=TAXONOMY)
        q = review_queue(self.cons._conn, self.rev, self.prom, tier=1)
        self.assertEqual([b["project"] for b in q["buckets"]], ["openclaw", "condo"])

    def test_tier2_counted_but_not_queued(self):
        self.journal.append(ev("e0")); self.journal.append(ev("e1"))
        self._reason_row("r:0::reasoning-episode@0.2", episode(kind="decision", evidence=["e0"]))
        self._reason_row("r:1::reasoning-episode@0.2", episode(kind="investigation", evidence=["e1"]))
        projects.backfill(self.cons._conn, dry_run=False, taxonomy=TAXONOMY)

        q = review_queue(self.cons._conn, self.rev, self.prom, tier=1)
        self.assertEqual(q["episode_count"], 1)
        self.assertEqual(q["tier2_total"], 1)
        self.assertEqual(q["buckets"][0]["tier2_count"], 1)

    def test_reviewed_episodes_leave_the_queue(self):
        ids = self._seed(2)
        actions.approve_episode(self.rev, ids[0])
        q = review_queue(self.cons._conn, self.rev, self.prom, tier=1)
        self.assertEqual(q["episode_count"], 1)
        self.assertEqual(q["already_reviewed"], 1)
        q_all = review_queue(self.cons._conn, self.rev, self.prom, tier=1, include_reviewed=True)
        self.assertEqual(q_all["episode_count"], 2)

    def test_export_payload_inlines_evidence(self):
        self.journal.append(ev("e0", text="the actual turn text"))
        self._reason_row("r:0::reasoning-episode@0.2", episode(evidence=["e0"]))
        projects.backfill(self.cons._conn, dry_run=False, taxonomy=TAXONOMY)
        q = review_queue(self.cons._conn, self.rev, self.prom, tier=1, include_evidence=True)
        ep = q["buckets"][0]["episodes"][0]
        self.assertEqual(ep["evidence"][0]["text"], "the actual turn text")
        self.assertEqual(ep["rationale"], "the journal must survive a crash mid-write")

    def test_project_filter(self):
        for i, thread in enumerate(["openclaw-a", "condo-board-letter"]):
            self.journal.append(ev(f"e{i}"))
            self._reason_row(f"r:{i}::reasoning-episode@0.2", episode(evidence=[f"e{i}"], thread=thread))
        projects.backfill(self.cons._conn, dry_run=False, taxonomy=TAXONOMY)
        q = review_queue(self.cons._conn, self.rev, self.prom, tier=1, projects=["condo"])
        self.assertEqual(q["episode_count"], 1)
        self.assertEqual(q["buckets"][0]["project"], "condo")


# ---------------------------------------------------------------------------
# Acceptance 5 — every mutation writes an audit row
# ---------------------------------------------------------------------------
class TestAuditChokepoint(MS6Base):
    def test_each_action_type_writes_actor_time_reason_and_prior_state(self):
        ids = self._seed(3)
        actions.approve_episode(self.rev, ids[0], reviewer="todd", reason="architecture decision")
        actions.reject_episode(self.rev, ids[1], reviewer="todd", reason="ephemeral task step")
        actions.defer_episode(self.rev, ids[2], reviewer="todd", reason="unclear, revisit")

        for mid, action, state in [
            (ids[0], "approve_episode", APPROVED),
            (ids[1], "reject_episode", REJECTED),
            (ids[2], "defer_episode", DEFERRED),
        ]:
            rows = self.rev.audit_for(mid)
            self.assertEqual(len(rows), 1, f"{action} wrote no audit row")
            row = rows[0]
            self.assertEqual(row["action"], action)
            self.assertEqual(row["actor"], "todd")
            self.assertTrue(row["at"])
            self.assertTrue(row["reason"])
            self.assertEqual(json.loads(row["new_state_json"])["review_state"], state)
            self.assertEqual(json.loads(row["prior_state_json"])["review_state"], PENDING)
            self.assertEqual(self.rev.state_of(mid), state)

    def test_prior_state_captures_what_the_verdict_replaced(self):
        ids = self._seed(1)
        actions.approve_episode(self.rev, ids[0], reason="first call")
        actions.reject_episode(self.rev, ids[0], reason="changed my mind")

        audit = self.rev.audit_for(ids[0])
        self.assertEqual(len(audit), 2)
        self.assertEqual(json.loads(audit[1]["prior_state_json"])["review_state"], APPROVED)
        self.assertEqual(self.rev.state_of(ids[0]), REJECTED)

    def test_invalid_state_is_rejected_at_the_chokepoint(self):
        with self.assertRaises(ValueError):
            self.rev.record("m", "bogus", "maybe", "todd")

    def test_unreviewed_memory_reads_as_pending(self):
        self.assertEqual(self.rev.state_of("never-seen"), PENDING)

    def test_apply_verdicts_reports_bad_rows_without_failing_the_batch(self):
        ids = self._seed(2)
        result = actions.apply_verdicts(
            self.rev,
            [
                {"memory_id": ids[0], "verdict": "approved"},
                {"memory_id": ids[1], "verdict": "nonsense"},
            ],
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(self.rev.state_of(ids[0]), APPROVED)
        self.assertEqual(self.rev.state_of(ids[1]), PENDING)


# ---------------------------------------------------------------------------
# expand_evidence — widening a citation without touching a review verdict
# ---------------------------------------------------------------------------
class TestExpandEvidence(MS6Base):
    def test_appends_connecting_turns_in_order(self):
        for eid in ("e0", "e1", "e2"):
            self.journal.append(ev(eid))
        self._reason_row("r:0::reasoning-episode@0.2", episode(evidence=["e1"]))
        result = actions.expand_evidence(
            self.cons._conn, self.rev, "r:0::reasoning-episode@0.2", ["e0", "e2"],
            reason="connecting turns for a fragmented narrative", dry_run=False,
        )
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["added"], ["e0", "e2"])
        row = self.cons._conn.execute(
            "SELECT evidence_event_ids_json FROM derived_memories WHERE memory_id='r:0::reasoning-episode@0.2'"
        ).fetchone()
        self.assertEqual(json.loads(row["evidence_event_ids_json"]), ["e1", "e0", "e2"])

    def test_does_not_touch_review_state(self):
        ids = self._seed(1)
        actions.expand_evidence(self.cons._conn, self.rev, ids[0], [], reason="x", dry_run=False)
        self.assertEqual(self.rev.state_of(ids[0]), PENDING)

    def test_writes_an_audit_entry_without_a_reviews_row(self):
        self.journal.append(ev("e0")); self.journal.append(ev("e5"))
        self._reason_row("r:0::reasoning-episode@0.2", episode(evidence=["e0"]))
        actions.expand_evidence(
            self.cons._conn, self.rev, "r:0::reasoning-episode@0.2", ["e5"], reason="x", dry_run=False
        )
        self.assertIsNone(self.rev.get("r:0::reasoning-episode@0.2"))
        audit = self.rev.audit_for("r:0::reasoning-episode@0.2")
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["action"], "expand_evidence")
        self.assertEqual(json.loads(audit[0]["prior_state_json"])["evidence_event_ids"], ["e0"])
        self.assertEqual(json.loads(audit[0]["new_state_json"])["added"], ["e5"])

    def test_refuses_an_event_from_a_different_conversation(self):
        self.journal.append(ev("e0"))
        self.journal.append(SourceEvent(
            schema_version="1.0", event_id="foreign", event_type="turn.completed",
            source=SourceProvenance(harness="claude", conversation_id="other-conv", turn_id="foreign"),
            observed_at=BASE, content={"text": "x"}, content_hash=compute_content_hash({"text": "x"}),
            actor_type="user",
        ))
        self._reason_row("r:0::reasoning-episode@0.2", episode(evidence=["e0"]))
        result = actions.expand_evidence(
            self.cons._conn, self.rev, "r:0::reasoning-episode@0.2", ["foreign"], reason="x", dry_run=False
        )
        self.assertIn("error", result)
        self.assertEqual(result["bad_event_ids"], ["foreign"])
        row = self.cons._conn.execute(
            "SELECT evidence_event_ids_json FROM derived_memories WHERE memory_id='r:0::reasoning-episode@0.2'"
        ).fetchone()
        self.assertEqual(json.loads(row["evidence_event_ids_json"]), ["e0"])

    def test_dry_run_by_default_and_deduplicates(self):
        self.journal.append(ev("e0")); self.journal.append(ev("e1"))
        self._reason_row("r:0::reasoning-episode@0.2", episode(evidence=["e0"]))
        dry = actions.expand_evidence(self.cons._conn, self.rev, "r:0::reasoning-episode@0.2", ["e1"], reason="x")
        self.assertTrue(dry["dry_run"])
        row = self.cons._conn.execute(
            "SELECT evidence_event_ids_json FROM derived_memories WHERE memory_id='r:0::reasoning-episode@0.2'"
        ).fetchone()
        self.assertEqual(json.loads(row["evidence_event_ids_json"]), ["e0"], "dry run must not write")

        actions.expand_evidence(self.cons._conn, self.rev, "r:0::reasoning-episode@0.2", ["e0", "e1"],
                                 reason="x", dry_run=False)
        already = actions.expand_evidence(self.cons._conn, self.rev, "r:0::reasoning-episode@0.2", ["e0", "e1"],
                                           reason="x", dry_run=False)
        self.assertEqual(already["dry_run"], True, "nothing new to add should short-circuit as a no-op")

    def test_unknown_memory_id_is_reported_not_raised(self):
        self.assertIn("error", actions.expand_evidence(self.cons._conn, self.rev, "nope", [], reason="x"))


# ---------------------------------------------------------------------------
# Bulk governance — what makes the ~25,900-row heuristic pile tractable
# ---------------------------------------------------------------------------
class TestBulkActions(MS6Base):
    def _heuristic_pile(self, n=5):
        for i in range(n):
            self.journal.append(ev(f"h{i}"))
            self._heuristic_row(f"h:{i}::heuristic-pattern@1.2", f"h{i}")

    def test_bulk_reject_defaults_to_dry_run(self):
        self._heuristic_pile(3)
        result = actions.bulk_reject(self.cons._conn, self.rev, reason="policy superseded")
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["matched"], 3)
        self.assertEqual(self.rev.counts(), {})

    def test_bulk_reject_writes_one_audit_row_for_many_memories(self):
        self._heuristic_pile(5)
        result = actions.bulk_reject(
            self.cons._conn, self.rev, reason="heuristic-pattern v1 ambiguous bucket; policy superseded",
            dry_run=False,
        )
        self.assertEqual(result["affected"], 5)
        audit = self.rev.audit_batch(result["batch_id"])
        self.assertEqual(len(audit), 1, "a bulk action must not write one audit row per memory")
        self.assertEqual(audit[0]["affected_count"], 5)
        self.assertIsNone(audit[0]["memory_id"])
        # per-row verdicts still land, so the queue agrees with the audit
        self.assertEqual(self.rev.counts()[REJECTED], 5)

    def test_bulk_reject_audit_records_what_reverses_it(self):
        self._heuristic_pile(4)
        result = actions.bulk_reject(
            self.cons._conn, self.rev, reason="policy superseded", category="ambiguous", dry_run=False
        )
        prior = json.loads(self.rev.audit_batch(result["batch_id"])[0]["prior_state_json"])
        self.assertEqual(prior["policy_name"], "heuristic-pattern")
        self.assertEqual(prior["category"], "ambiguous")
        self.assertEqual(prior["matched"], 4)
        self.assertEqual(prior["prior_state_histogram"], {"queued_for_review": 4})

    def test_bulk_reject_requires_a_reason(self):
        self._heuristic_pile(2)
        with self.assertRaises(ValueError):
            actions.bulk_reject(self.cons._conn, self.rev, reason="", dry_run=False)

    def test_bulk_reject_flips_approval_state_so_queues_agree(self):
        self._heuristic_pile(3)
        actions.bulk_reject(self.cons._conn, self.rev, reason="superseded", dry_run=False)
        remaining = self.cons._conn.execute(
            "SELECT COUNT(*) FROM derived_memories WHERE policy_name='heuristic-pattern' AND approval_state='queued_for_review'"
        ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_bulk_reject_never_touches_reasoning_episodes(self):
        self._heuristic_pile(3)
        ids = self._seed(2)
        actions.bulk_reject(self.cons._conn, self.rev, reason="superseded", dry_run=False)
        for mid in ids:
            self.assertEqual(self.rev.state_of(mid), PENDING)
        q = review_queue(self.cons._conn, self.rev, self.prom, tier=1)
        self.assertEqual(q["episode_count"], 2)

    def test_before_date_filter_scopes_the_bulk_action(self):
        self.journal.append(ev("d0"))
        self._heuristic_row("h:dated::heuristic-pattern@1.2", "d0", event_date=BASE)  # 2026-05-01
        early = actions.bulk_reject(self.cons._conn, self.rev, reason="x", before_date="2026-01-01")
        self.assertEqual(early["matched"], 0)
        late = actions.bulk_reject(self.cons._conn, self.rev, reason="x", before_date="2027-01-01")
        self.assertEqual(late["matched"], 1)

    def test_date_filter_never_sweeps_rows_with_an_unknown_date(self):
        # A NULL event_date is not evidence that the row predates the cutoff.
        # The production pile carries dates from 2000 to 2029, some plainly
        # wrong, so a date-scoped bulk action must not guess.
        self._heuristic_pile(3)  # all NULL-dated
        scoped = actions.bulk_reject(self.cons._conn, self.rev, reason="x", before_date="2030-01-01")
        self.assertEqual(scoped["matched"], 0)
        unscoped = actions.bulk_reject(self.cons._conn, self.rev, reason="x")
        self.assertEqual(unscoped["matched"], 3)

    def test_retires_only_older_policy_versions_of_the_same_event(self):
        # The production pile is 25,961 queued rows over 9,757 distinct
        # events: the same turns re-judged under v1.0, v1.1 and v1.2, every
        # pass left queued. Retiring the stale ones is bookkeeping — the
        # newest verdict per event must survive.
        self.journal.append(ev("e0")); self.journal.append(ev("e1"))
        for version in ("1.0", "1.1", "1.2"):
            self.cons.record_consolidation(
                job_id=f"job:e0@{version}", memory_id=f"h:e0@{version}", source_event_id="e0",
                policy_name="heuristic-pattern", policy_version=version,
                result=ExtractionResult(
                    category=ExtractionCategory.AMBIGUOUS, statement="a turn", reason="x", confidence=0.2
                ),
                approval_state="queued_for_review", supersedes=None,
            )
        # an event only ever judged once must be left entirely alone
        self._heuristic_row("h:e1@1.0", "e1")

        result = actions.bulk_reject_stale_policy_versions(self.cons._conn, self.rev, dry_run=False)
        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["stale_by_version"], {"1.0": 1, "1.1": 1})
        self.assertEqual(result["distinct_events"], 2)

        still_queued = {
            r["memory_id"]
            for r in self.cons._conn.execute(
                "SELECT memory_id FROM derived_memories WHERE approval_state='queued_for_review'"
            )
        }
        self.assertEqual(still_queued, {"h:e0@1.2", "h:e1@1.0"})

    def test_newest_version_is_found_even_when_it_left_the_queue(self):
        """The newest row for an event is often no longer queued.

        MS3.6's coverage pass flips it to `superseded_by_reasoning`. Scoping
        the newest-version lookup to queued rows makes that row invisible,
        so an older version reads as newest and its stale row survives for
        an event that is already resolved — this is the production shape:
        3,175 events carried v1.0+v1.1 queued with v1.2 superseded.
        """
        self.journal.append(ev("e0"))
        for version, state in (("1.0", "queued_for_review"),
                               ("1.1", "queued_for_review"),
                               ("1.2", "superseded_by_reasoning")):
            self.cons.record_consolidation(
                job_id=f"job:e0@{version}", memory_id=f"h:e0@{version}", source_event_id="e0",
                policy_name="heuristic-pattern", policy_version=version,
                result=ExtractionResult(
                    category=ExtractionCategory.AMBIGUOUS, statement="a turn", reason="x", confidence=0.2
                ),
                approval_state=state, supersedes=None,
            )

        result = actions.bulk_reject_stale_policy_versions(self.cons._conn, self.rev, dry_run=False)
        self.assertEqual(result["matched"], 2, "both queued rows are stale — v1.2 already judged this event")
        self.assertEqual(result["kept_versions"], [])
        left = self.cons._conn.execute(
            "SELECT COUNT(*) FROM derived_memories WHERE approval_state='queued_for_review'"
        ).fetchone()[0]
        self.assertEqual(left, 0)

    def test_auto_accepted_newer_version_also_retires_older_queued_rows(self):
        self.journal.append(ev("e0"))
        for version, state in (("1.0", "queued_for_review"), ("1.1", "auto_accepted")):
            self.cons.record_consolidation(
                job_id=f"job:e0@{version}", memory_id=f"h:e0@{version}", source_event_id="e0",
                policy_name="heuristic-pattern", policy_version=version,
                result=ExtractionResult(
                    category=ExtractionCategory.DURABLE_CANDIDATE, statement="a turn", reason="x", confidence=0.8
                ),
                approval_state=state, supersedes=None,
            )
        result = actions.bulk_reject_stale_policy_versions(self.cons._conn, self.rev, dry_run=False)
        self.assertEqual(result["matched"], 1)

    def test_version_ordering_is_numeric_not_lexical(self):
        self.assertGreater(actions._version_key("1.10"), actions._version_key("1.9"))
        self.assertEqual(actions._version_key("garbage"), (0,))

    def test_confirm_superseded_is_one_action(self):
        for i in range(4):
            self.journal.append(ev(f"h{i}"))
            self._heuristic_row(f"h:{i}::heuristic-pattern@1.2", f"h{i}", state="superseded_by_reasoning")
        result = actions.bulk_confirm_superseded(self.cons._conn, self.rev, dry_run=False)
        self.assertEqual(result["affected"], 4)
        self.assertEqual(len(self.rev.audit_batch(result["batch_id"])), 1)
        self.assertEqual(self.rev.counts()[DEFERRED], 4)

    def test_revert_restores_the_prior_approval_state(self):
        # The claim that bulk rejection is safe rests entirely on this
        # round-tripping. If it does not, the bulk action is not reversible
        # and should not be offered.
        self._heuristic_pile(5)
        result = actions.bulk_reject(self.cons._conn, self.rev, reason="policy superseded", dry_run=False)
        self.assertEqual(self.rev.counts()[REJECTED], 5)

        dry = actions.revert_batch(self.cons._conn, self.rev, result["batch_id"])
        self.assertEqual(dry["would_revert"], 5)
        self.assertEqual(dry["restore_to"], "queued_for_review")

        actions.revert_batch(self.cons._conn, self.rev, result["batch_id"], dry_run=False)
        restored = self.cons._conn.execute(
            "SELECT COUNT(*) FROM derived_memories WHERE approval_state='queued_for_review'"
        ).fetchone()[0]
        self.assertEqual(restored, 5)
        self.assertEqual(self.rev.counts().get(REJECTED, 0), 0)

    def test_revert_refuses_a_batch_whose_prior_state_was_mixed(self):
        self._heuristic_pile(2)
        result = actions.bulk_reject(self.cons._conn, self.rev, reason="x", dry_run=False)
        # corrupt the histogram to simulate a population written by something else
        self.rev.conn.execute(
            "UPDATE review_audit SET prior_state_json = ? WHERE batch_id = ?",
            (json.dumps({"prior_state_histogram": {"queued_for_review": 1, "auto_accepted": 1}}), result["batch_id"]),
        )
        self.rev.conn.commit()
        out = actions.revert_batch(self.cons._conn, self.rev, result["batch_id"], dry_run=False)
        self.assertEqual(out["reverted"], 0)
        self.assertIn("refusing to guess", out["error"])

    def test_revert_of_unknown_batch_is_reported_not_raised(self):
        self.assertEqual(actions.revert_batch(self.cons._conn, self.rev, "nope")["reverted"], 0)

    def test_sample_audit_draws_from_the_batch(self):
        self._heuristic_pile(10)
        result = actions.bulk_reject(self.cons._conn, self.rev, reason="superseded", dry_run=False)
        sample = actions.sample_audit(self.cons._conn, result["batch_id"], self.rev, n=4)
        self.assertEqual(len(sample), 4)
        self.assertTrue(all("statement" in s for s in sample))


# ---------------------------------------------------------------------------
# Acceptance 2 — approval promotes exactly what was approved, idempotently
# ---------------------------------------------------------------------------
class TestPromoteApproved(MS6Base):
    async def test_promotes_only_approved_and_is_idempotent(self):
        ids = self._seed(3)
        actions.approve_episode(self.rev, ids[0])
        actions.approve_episode(self.rev, ids[1])
        actions.reject_episode(self.rev, ids[2], reason="ephemeral")

        dry = await actions.promote_approved(
            self.cons, self.journal, self.prom, self.rev, self.ok_remember, dry_run=True
        )
        self.assertEqual(dry["eligible_this_run"], 2)
        self.assertEqual(len(self.calls), 0)

        live = await actions.promote_approved(
            self.cons, self.journal, self.prom, self.rev, self.ok_remember, dry_run=False, inter_call_delay=0
        )
        self.assertEqual(len(live["promoted"]), 2)
        self.assertEqual(len(self.calls), 2)

        again = await actions.promote_approved(
            self.cons, self.journal, self.prom, self.rev, self.ok_remember, dry_run=False, inter_call_delay=0
        )
        self.assertEqual(len(again.get("promoted", [])), 0)
        self.assertEqual(len(self.calls), 2, "a second run must not re-issue remember()")

    async def test_rejected_episode_is_never_promoted(self):
        ids = self._seed(2)
        actions.reject_episode(self.rev, ids[0], reason="ephemeral")
        actions.reject_episode(self.rev, ids[1], reason="ephemeral")
        result = await actions.promote_approved(
            self.cons, self.journal, self.prom, self.rev, self.ok_remember, dry_run=False, inter_call_delay=0
        )
        self.assertEqual(result["promoted"], [])
        self.assertEqual(len(self.calls), 0)

    async def test_verdicts_survive_a_promotion_that_stops_early(self):
        # Approval lives in `reviews`, promotion in `promotions` — a quota
        # stop must lose neither.
        ids = self._seed(3)
        for mid in ids:
            actions.approve_episode(self.rev, mid)
        await actions.promote_approved(
            self.cons, self.journal, self.prom, self.rev, self.ok_remember,
            dry_run=False, limit=1, inter_call_delay=0,
        )
        self.assertEqual(self.rev.counts()[APPROVED], 3)
        rest = await actions.promote_approved(
            self.cons, self.journal, self.prom, self.rev, self.ok_remember, dry_run=False, inter_call_delay=0
        )
        self.assertEqual(len(rest["promoted"]), 2)


if __name__ == "__main__":
    unittest.main()
