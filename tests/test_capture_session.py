"""MS4a2 — capture_session: live episode/wiki-proposal capture from Cowork.

Uses the same lightweight MCP session fakes as tests/test_capture.py.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from server.capture.session_capture import capture_session
from server.consolidation.store import ConsolidationStore
from tests.test_capture import fake_client_info, fake_session


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


class TestCaptureSessionEpisodes(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "journal.db"
        self.session = fake_session(client_info=fake_client_info("Claude Desktop"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def _episode_item(self, **overrides):
        item = {
            "destination": "episode",
            "statement": "Decided to use PostgreSQL for the new service.",
            "reasoning_kind": "decision",
            "confidence": 0.9,
            "evidence_text": "User said: let's go with PostgreSQL, it's what the team already knows.",
        }
        item.update(overrides)
        return item

    def test_valid_episode_item_stages_for_review(self):
        results = capture_session(
            items=[self._episode_item()],
            project="atlas",
            source_description="planning session",
            session=self.session,
            request_id="req1",
            db_path=self.db_path,
        )
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.destination, "episode")
        self.assertIsNotNone(r.memory_id)

        with ConsolidationStore(db_path=self.db_path) as store:
            row = store.get_derived_memory(r.memory_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["policy_name"], "cowork_live_v1")
        self.assertEqual(row["approval_state"], "queued_for_review")
        self.assertEqual(row["reasoning_kind"], "decision")

    def test_evidence_text_is_journaled_as_a_real_event(self):
        results = capture_session(
            items=[self._episode_item()],
            project=None, source_description=None,
            session=self.session, request_id="req1", db_path=self.db_path,
        )
        with ConsolidationStore(db_path=self.db_path) as store:
            row = store.get_derived_memory(results[0].memory_id)
            evidence_ids = json.loads(row["evidence_event_ids_json"])
            self.assertEqual(len(evidence_ids), 1)

            # the cited event actually exists in the events table (same journal.db)
            event_row = store._conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (evidence_ids[0],)
            ).fetchone()
        self.assertIsNotNone(event_row)
        content = json.loads(event_row["content_json"])
        self.assertIn("PostgreSQL", content["evidence"])

    def test_default_threshold_never_auto_accepts(self):
        os.environ.pop("CMF_REASONING_AUTO_ACCEPT_THRESHOLD", None)
        results = capture_session(
            items=[self._episode_item(confidence=0.99)],
            project=None, source_description=None,
            session=self.session, request_id="req1", db_path=self.db_path,
        )
        with ConsolidationStore(db_path=self.db_path) as store:
            row = store.get_derived_memory(results[0].memory_id)
        self.assertEqual(row["approval_state"], "queued_for_review")

    def test_threshold_set_auto_accepts_above_it(self):
        os.environ["CMF_REASONING_AUTO_ACCEPT_THRESHOLD"] = "0.8"
        try:
            results = capture_session(
                items=[self._episode_item(confidence=0.9), self._episode_item(confidence=0.5)],
                project=None, source_description=None,
                session=self.session, request_id="req1", db_path=self.db_path,
            )
        finally:
            del os.environ["CMF_REASONING_AUTO_ACCEPT_THRESHOLD"]

        with ConsolidationStore(db_path=self.db_path) as store:
            high = store.get_derived_memory(results[0].memory_id)
            low = store.get_derived_memory(results[1].memory_id)
        self.assertEqual(high["approval_state"], "auto_accepted")
        self.assertEqual(low["approval_state"], "queued_for_review")

    def test_secret_in_evidence_text_is_redacted(self):
        results = capture_session(
            items=[self._episode_item(evidence_text="the API key is AIzaSyD-FAKE1234567890EXAMPLEKEYXXXXXXX")],
            project=None, source_description=None,
            session=self.session, request_id="req1", db_path=self.db_path,
        )
        with ConsolidationStore(db_path=self.db_path) as store:
            row = store.get_derived_memory(results[0].memory_id)
            evidence_id = json.loads(row["evidence_event_ids_json"])[0]
            event_row = store._conn.execute("SELECT content_json FROM events WHERE event_id = ?", (evidence_id,)).fetchone()
        content = json.loads(event_row["content_json"])
        self.assertNotIn("AIzaSyD-FAKE1234567890EXAMPLEKEYXXXXXXX", content["evidence"])

    def test_missing_statement_reported_not_raised(self):
        results = capture_session(
            items=[self._episode_item(statement="")],
            project=None, source_description=None,
            session=self.session, request_id="req1", db_path=self.db_path,
        )
        self.assertFalse(results[0].ok)
        self.assertIn("statement", results[0].error)

    def test_invalid_reasoning_kind_reported(self):
        results = capture_session(
            items=[self._episode_item(reasoning_kind="not_a_real_kind")],
            project=None, source_description=None,
            session=self.session, request_id="req1", db_path=self.db_path,
        )
        self.assertFalse(results[0].ok)
        self.assertIn("reasoning_kind", results[0].error)

    def test_out_of_range_confidence_reported(self):
        results = capture_session(
            items=[self._episode_item(confidence=1.5)],
            project=None, source_description=None,
            session=self.session, request_id="req1", db_path=self.db_path,
        )
        self.assertFalse(results[0].ok)

    def test_unknown_destination_reported(self):
        results = capture_session(
            items=[{"destination": "carrier_pigeon", "evidence_text": "x"}],
            project=None, source_description=None,
            session=self.session, request_id="req1", db_path=self.db_path,
        )
        self.assertFalse(results[0].ok)
        self.assertIn("destination", results[0].error)

    def test_one_bad_item_does_not_block_the_rest(self):
        results = capture_session(
            items=[self._episode_item(statement=""), self._episode_item()],
            project=None, source_description=None,
            session=self.session, request_id="req1", db_path=self.db_path,
        )
        self.assertFalse(results[0].ok)
        self.assertTrue(results[1].ok)


class TestCaptureSessionDocProposals(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "journal.db"
        self.wiki_root = Path(self.temp_dir.name) / "wiki"
        self.proposals_dir = Path(self.temp_dir.name) / "doc-proposals"
        (self.wiki_root / "WIKI").mkdir(parents=True)
        _git("init", "-q", cwd=self.wiki_root)
        _git("config", "user.email", "test@example.com", cwd=self.wiki_root)
        _git("config", "user.name", "Test", cwd=self.wiki_root)
        (self.wiki_root / "WIKI" / ".gitkeep").write_text("")
        _git("add", "-A", cwd=self.wiki_root)
        _git("commit", "-q", "-m", "initial", cwd=self.wiki_root)
        self.session = fake_session(client_info=fake_client_info("Claude Desktop"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_valid_wiki_item_creates_a_proposal(self):
        results = capture_session(
            items=[{
                "destination": "doc_proposal",
                "target_path": "WIKI/new-topic.md",
                "proposed_content": "# New Topic\nSome durable content.",
                "doc_rationale": "Worth keeping as reference.",
                "evidence_text": "We discussed this topic at length.",
            }],
            project=None, source_description=None,
            session=self.session, request_id="req1",
            db_path=self.db_path, wiki_root=self.wiki_root, proposals_dir=self.proposals_dir,
        )
        self.assertTrue(results[0].ok, results[0].error)
        self.assertIsNotNone(results[0].proposal_id)
        self.assertTrue((self.proposals_dir / f"{results[0].proposal_id}.json").exists())

    def test_missing_doc_rationale_reported(self):
        results = capture_session(
            items=[{
                "destination": "doc_proposal",
                "target_path": "WIKI/new-topic.md",
                "proposed_content": "# New Topic\nSome content.",
                "evidence_text": "x",
            }],
            project=None, source_description=None,
            session=self.session, request_id="req1",
            db_path=self.db_path, wiki_root=self.wiki_root, proposals_dir=self.proposals_dir,
        )
        self.assertFalse(results[0].ok)
        self.assertIn("doc_rationale", results[0].error)

    def test_mixed_batch_routes_each_item_independently(self):
        results = capture_session(
            items=[
                {
                    "destination": "episode",
                    "statement": "Chose FastAPI over Flask for the new service.",
                    "reasoning_kind": "decision",
                    "confidence": 0.85,
                    "evidence_text": "We decided on FastAPI for async support.",
                },
                {
                    "destination": "doc_proposal",
                    "target_path": "WIKI/new-topic.md",
                    "proposed_content": "# New Topic\nSome durable content.",
                    "doc_rationale": "Reusable reference material.",
                    "evidence_text": "Durable knowledge worth keeping.",
                },
            ],
            project=None, source_description=None,
            session=self.session, request_id="req1",
            db_path=self.db_path, wiki_root=self.wiki_root, proposals_dir=self.proposals_dir,
        )
        self.assertEqual([r.destination for r in results], ["episode", "doc_proposal"])
        self.assertTrue(all(r.ok for r in results))


if __name__ == "__main__":
    unittest.main()
