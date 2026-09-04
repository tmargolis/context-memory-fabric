"""Milestone 2 importer tests: ChatGPT, Claude, and Gemini evidence emission.

Covers acceptance test 1 (idempotent re-import) at the importer level for
all three sources. Claude and Gemini tests run against the real exports
Todd downloaded on 2026-09-03 (imports/source/{claude,gemini}-export/) when
present, and skip cleanly otherwise so the suite still passes on a machine
without those files (e.g. CI, or a fresh clone).
"""

import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from server.importers.chatgpt import journal_chatgpt_export
from server.importers.claude import journal_claude_export
from server.importers.gemini import journal_gemini_apps_export, journal_gemini_workspace_export
from server.journal.store import SqliteEventStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_EXPORT_DIR = PROJECT_ROOT / "imports" / "source" / "claude-export"
CLAUDE_CONVERSATIONS_PATH = CLAUDE_EXPORT_DIR / "conversations.json"
CLAUDE_PROJECTS_PATH = CLAUDE_EXPORT_DIR / "projects"
CLAUDE_MEMORIES_PATH = CLAUDE_EXPORT_DIR / "memories"
GEMINI_WORKSPACE_DIR = (
    PROJECT_ROOT / "imports" / "source" / "gemini-export" / "Takeout" / "Gemini in Workspace" / "Conversation History"
)
GEMINI_APPS_MY_ACTIVITY_PATH = (
    PROJECT_ROOT / "imports" / "source" / "gemini-export" / "Takeout 3" / "My Activity" / "Gemini Apps" / "MyActivity.json"
)


def make_synthetic_chatgpt_export(tmpdir: Path) -> Path:
    """A minimal but structurally real ChatGPT native export: one linear
    conversation, one branchy conversation (a discarded sibling node must
    NOT be journaled), and one is_do_not_remember conversation (must be
    skipped entirely).
    """
    conversations = [
        {
            "id": "conv-linear",
            "title": "Linear conversation",
            "current_node": "n2",
            "mapping": {
                "n1": {
                    "message": {
                        "id": "m1",
                        "author": {"role": "user"},
                        "create_time": 1700000000.0,
                        "content": {"parts": ["Hello there"]},
                    },
                    "parent": None,
                },
                "n2": {
                    "message": {
                        "id": "m2",
                        "author": {"role": "assistant"},
                        "create_time": 1700000010.0,
                        "content": {"parts": ["Hi! How can I help?"]},
                    },
                    "parent": "n1",
                },
            },
        },
        {
            "id": "conv-branchy",
            "title": "Branchy conversation",
            "current_node": "b2",
            "mapping": {
                "b1": {
                    "message": {
                        "id": "bm1",
                        "author": {"role": "user"},
                        "create_time": 1700001000.0,
                        "content": {"parts": ["First question"]},
                    },
                    "parent": None,
                },
                "b2": {
                    "message": {
                        "id": "bm2",
                        "author": {"role": "assistant"},
                        "create_time": 1700001010.0,
                        "content": {"parts": ["Active branch answer"]},
                    },
                    "parent": "b1",
                },
                "b2-alt": {
                    "message": {
                        "id": "bm2-alt",
                        "author": {"role": "assistant"},
                        "create_time": 1700001011.0,
                        "content": {"parts": ["Abandoned regenerated answer"]},
                    },
                    "parent": "b1",
                },
            },
        },
        {
            "id": "conv-private",
            "title": "Should be skipped",
            "is_do_not_remember": True,
            "current_node": "p1",
            "mapping": {
                "p1": {
                    "message": {
                        "id": "pm1",
                        "author": {"role": "user"},
                        "create_time": 1700002000.0,
                        "content": {"parts": ["This should never be journaled"]},
                    },
                    "parent": None,
                }
            },
        },
    ]
    path = tmpdir / "conversations-000.json"
    path.write_text(json.dumps(conversations))
    return path


class TestChatGPTEvidenceEmission(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.export_path = make_synthetic_chatgpt_export(self.tmp_path)
        self.db_path = self.tmp_path / "journal.db"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_journals_active_path_messages_only(self):
        with SqliteEventStore(self.db_path) as store:
            stats = journal_chatgpt_export([str(self.export_path)], store, allowed_root=self.tmp_path)
            self.assertEqual(stats["conversations_processed"], 2)  # excludes do_not_remember
            self.assertEqual(stats["conversations_skipped_do_not_remember"], 1)
            self.assertEqual(stats["events_journaled"], 4)  # 2 linear + 2 branchy active-path

            self.assertIsNotNone(store.get("chatgpt:conv-linear:m1"))
            self.assertIsNotNone(store.get("chatgpt:conv-linear:m2"))
            self.assertIsNotNone(store.get("chatgpt:conv-branchy:bm2"))
            self.assertIsNone(store.get("chatgpt:conv-branchy:bm2-alt"), "Discarded branch must not be journaled")
            self.assertIsNone(store.get("chatgpt:conv-private:pm1"), "is_do_not_remember conversation must be skipped")

    def test_reimporting_the_same_file_produces_zero_new_events(self):
        with SqliteEventStore(self.db_path) as store:
            journal_chatgpt_export([str(self.export_path)], store, allowed_root=self.tmp_path)
            second = journal_chatgpt_export([str(self.export_path)], store, allowed_root=self.tmp_path)
            self.assertEqual(second["events_journaled"], 0)
            self.assertEqual(second["events_deduped"], 4)

    def test_content_and_actor_type_preserved(self):
        with SqliteEventStore(self.db_path) as store:
            journal_chatgpt_export([str(self.export_path)], store, allowed_root=self.tmp_path)
            event = store.get("chatgpt:conv-linear:m1")
            self.assertEqual(event.content["text"], "Hello there")
            self.assertEqual(event.actor_type, "user")
            assistant_event = store.get("chatgpt:conv-linear:m2")
            self.assertEqual(assistant_event.actor_type, "assistant")


@unittest.skipUnless(
    CLAUDE_CONVERSATIONS_PATH.exists() and CLAUDE_PROJECTS_PATH.exists(),
    f"Real Claude export not found at {CLAUDE_EXPORT_DIR}",
)
class TestClaudeEvidenceEmissionAgainstRealExport(unittest.TestCase):
    """Runs against Todd's real 2026-09-04 full Claude export (extracted
    plain files, not the original per-category zips — journal_claude_export
    accepts either). Skips cleanly when that export isn't present on disk
    (fresh clone, CI, etc.).
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "journal.db"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _run_import(self, store):
        return journal_claude_export(
            conversations_path=CLAUDE_CONVERSATIONS_PATH,
            projects_path=CLAUDE_PROJECTS_PATH,
            memories_path=CLAUDE_MEMORIES_PATH if CLAUDE_MEMORIES_PATH.exists() else None,
            export_created_at="2026-09-04T00:36:13.660803+00:00",
            store=store,
        )

    def test_journals_real_export_idempotently(self):
        with SqliteEventStore(self.db_path) as store:
            first = self._run_import(store)
            self.assertGreater(first["events_journaled"], 0)
            self.assertGreater(first["conversations_processed"], 0)
            self.assertGreater(first["projects_processed"], 0)

            second = self._run_import(store)
            self.assertEqual(second["events_journaled"], 0, "Re-import must produce zero new events")
            self.assertEqual(second["events_deduped"], first["events_journaled"] + first["events_deduped"])

    def test_branching_conversation_journals_only_the_latest_leaf_path(self):
        with SqliteEventStore(self.db_path) as store:
            self._run_import(store)
            # Every journaled turn event's parent (when set) must itself be
            # a journaled event in the same conversation, proving the walk
            # is a single connected path rather than every node in the tree.
            events = store.query(harness="claude")
            journaled_ids = {e.event_id for e in events}
            for event in events:
                for parent_id in event.parent_event_ids:
                    self.assertIn(parent_id, journaled_ids, f"{event.event_id}'s parent {parent_id} was not journaled")

    @unittest.skipUnless(CLAUDE_MEMORIES_PATH.exists(), f"Claude memories export not found at {CLAUDE_MEMORIES_PATH}")
    def test_memory_snapshots_are_attributed_to_assistant(self):
        """Claude's own synthesized summary of the user must be
        actor_type='assistant', not 'user' — see server.importers.claude's
        module docstring.
        """
        with SqliteEventStore(self.db_path) as store:
            stats = self._run_import(store)
            self.assertGreater(stats["memory_snapshots_seen"], 0)
            memory_events = [e for e in store.query(harness="claude") if e.event_type in ("memory_snapshot", "memory_file.snapshot")]
            self.assertTrue(memory_events)
            for event in memory_events:
                self.assertEqual(event.actor_type, "assistant")


@unittest.skipUnless(GEMINI_WORKSPACE_DIR.exists(), f"Real Gemini Workspace export not found at {GEMINI_WORKSPACE_DIR}")
class TestGeminiWorkspaceEvidenceEmissionAgainstRealExport(unittest.TestCase):
    """Runs against Todd's real 2026-09-03 Gemini-in-Workspace export
    (Conversation History .txt files) — a distinct product from "Gemini
    Apps" (see TestGeminiAppsEvidenceEmissionAgainstRealExport below),
    both journaled from the same Takeout request family.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "journal.db"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_journals_real_workspace_conversations_idempotently(self):
        with SqliteEventStore(self.db_path) as store:
            first = journal_gemini_workspace_export(GEMINI_WORKSPACE_DIR, store)
            self.assertGreater(first["events_journaled"], 0)
            second = journal_gemini_workspace_export(GEMINI_WORKSPACE_DIR, store)
            self.assertEqual(second["events_journaled"], 0)


@unittest.skipUnless(GEMINI_APPS_MY_ACTIVITY_PATH.exists(), f"Real Gemini Apps export not found at {GEMINI_APPS_MY_ACTIVITY_PATH}")
class TestGeminiAppsEvidenceEmissionAgainstRealExport(unittest.TestCase):
    """Runs against Todd's real 2026-09-04 "Gemini Apps" export — the main
    gemini.google.com conversation history that the original 2026-09-03
    Takeout request was missing entirely (see IMPLEMENTATION-PLAN.md's
    Milestone 2 notes for both rounds).
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "journal.db"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_journals_real_apps_activity_idempotently(self):
        with SqliteEventStore(self.db_path) as store:
            first = journal_gemini_apps_export(GEMINI_APPS_MY_ACTIVITY_PATH, store)
            self.assertGreater(first["prompt_events_journaled"], 0)
            self.assertGreater(first["response_events_journaled"], 0)

            second = journal_gemini_apps_export(GEMINI_APPS_MY_ACTIVITY_PATH, store)
            self.assertEqual(second["prompt_events_journaled"], 0)
            self.assertEqual(second["response_events_journaled"], 0)

    def test_response_events_reference_their_prompt_as_parent(self):
        with SqliteEventStore(self.db_path) as store:
            journal_gemini_apps_export(GEMINI_APPS_MY_ACTIVITY_PATH, store)
            events = store.query(harness="gemini")
            gemini_apps_events = [e for e in events if e.metadata.get("product") == "gemini_apps"]
            journaled_ids = {e.event_id for e in gemini_apps_events}
            responses = [e for e in gemini_apps_events if e.actor_type == "assistant"]
            self.assertTrue(responses)
            for response in responses:
                for parent_id in response.parent_event_ids:
                    self.assertIn(parent_id, journaled_ids)


if __name__ == "__main__":
    unittest.main()
