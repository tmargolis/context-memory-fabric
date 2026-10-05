"""MS6d — durable-knowledge proposal review/apply loop.

Validates the review -> apply split that is MS6d's whole safety property:
no single call gets from a fresh proposal to a canonical LLM_Wiki write.

- review_proposal(): approve/reject, notes, re-review refusal
- apply_proposal(): approval gate, both sha guards (stale base, drifted
  create-target), dry_run default, real write + git commit
- bulk_reject_proposals(): partial success (some ids already decided)
"""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server.proposals import (
    apply_proposal,
    bulk_reject_proposals,
    create_doc_proposal,
    get_proposal,
    list_proposals,
    review_proposal,
)


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


class TestMS6dProposalReview(unittest.TestCase):
    def setUp(self):
        self.temp_wiki = tempfile.TemporaryDirectory()
        self.wiki_root = Path(self.temp_wiki.name)

        self.temp_proposals = tempfile.TemporaryDirectory()
        self.proposals_dir = Path(self.temp_proposals.name) / "doc-proposals"

        wiki_dir = self.wiki_root / "WIKI"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        self.project_md = wiki_dir / "project.md"
        self.initial_content = "# Project Atlas\nProject Atlas uses PostgreSQL."
        self.project_md.write_text(self.initial_content, encoding="utf-8")

        # Real git repo, so apply_proposal's commit path is exercised for real,
        # not mocked — this is the first CMF code that writes into LLM_WIKI_PATH.
        _git("init", "-q", cwd=self.wiki_root)
        _git("config", "user.email", "test@example.com", cwd=self.wiki_root)
        _git("config", "user.name", "Test", cwd=self.wiki_root)
        _git("add", "-A", cwd=self.wiki_root)
        _git("commit", "-q", "-m", "initial", cwd=self.wiki_root)

    def tearDown(self):
        self.temp_wiki.cleanup()
        self.temp_proposals.cleanup()

    def _make_proposal(self, content="# Project Atlas\nProject Atlas uses PostgreSQL and Redis."):
        return create_doc_proposal(
            target_path="WIKI/project.md",
            proposed_content=content,
            rationale="Adding Redis for caching layer.",
            wiki_root=self.wiki_root,
            proposals_dir=self.proposals_dir,
        )

    # -- review_proposal ------------------------------------------------

    def test_review_approve_records_reviewer_and_notes(self):
        proposal = self._make_proposal()
        reviewed = review_proposal(
            proposal.proposal_id, "approved", reviewer="todd", notes="looks right",
            proposals_dir=self.proposals_dir,
        )
        self.assertEqual(reviewed.status, "approved")
        self.assertEqual(reviewed.reviewer, "todd")
        self.assertEqual(reviewed.review_notes, "looks right")
        self.assertIsNotNone(reviewed.reviewed_at)

        # persisted, not just returned
        reloaded = get_proposal(proposal.proposal_id, proposals_dir=self.proposals_dir)
        self.assertEqual(reloaded.status, "approved")

    def test_review_unknown_verdict_rejected(self):
        proposal = self._make_proposal()
        with self.assertRaises(ValueError):
            review_proposal(proposal.proposal_id, "maybe", proposals_dir=self.proposals_dir)

    def test_review_unknown_proposal_id_rejected(self):
        with self.assertRaises(ValueError):
            review_proposal("prop_does_not_exist", "approved", proposals_dir=self.proposals_dir)

    def test_re_review_refused_not_overwritten(self):
        """A second verdict on an already-decided proposal is refused, not silently applied."""
        proposal = self._make_proposal()
        review_proposal(proposal.proposal_id, "approved", notes="first", proposals_dir=self.proposals_dir)
        with self.assertRaises(ValueError):
            review_proposal(proposal.proposal_id, "rejected", notes="changed my mind", proposals_dir=self.proposals_dir)

        # first verdict survives untouched
        reloaded = get_proposal(proposal.proposal_id, proposals_dir=self.proposals_dir)
        self.assertEqual(reloaded.status, "approved")
        self.assertEqual(reloaded.review_notes, "first")

    # -- apply_proposal: the approval gate --------------------------------

    def test_apply_refused_without_approval(self):
        """The core safety property: no path from a fresh proposal straight to a write."""
        proposal = self._make_proposal()
        with self.assertRaises(ValueError):
            apply_proposal(
                proposal.proposal_id, expected_sha256=proposal.proposed_sha256,
                dry_run=False, wiki_root=self.wiki_root, proposals_dir=self.proposals_dir,
            )
        # file untouched
        self.assertEqual(self.project_md.read_text(), self.initial_content)

    def test_apply_refused_on_wrong_expected_sha(self):
        proposal = self._make_proposal()
        review_proposal(proposal.proposal_id, "approved", proposals_dir=self.proposals_dir)
        with self.assertRaises(ValueError):
            apply_proposal(
                proposal.proposal_id, expected_sha256="0" * 64,
                dry_run=False, wiki_root=self.wiki_root, proposals_dir=self.proposals_dir,
            )
        self.assertEqual(self.project_md.read_text(), self.initial_content)

    # -- apply_proposal: dry run vs real -------------------------------------

    def test_dry_run_writes_nothing(self):
        proposal = self._make_proposal()
        review_proposal(proposal.proposal_id, "approved", proposals_dir=self.proposals_dir)
        result = apply_proposal(
            proposal.proposal_id, expected_sha256=proposal.proposed_sha256,
            dry_run=True, wiki_root=self.wiki_root, proposals_dir=self.proposals_dir,
        )
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.project_md.read_text(), self.initial_content)
        # status stays 'approved', not advanced to 'applied' by a dry run
        reloaded = get_proposal(proposal.proposal_id, proposals_dir=self.proposals_dir)
        self.assertEqual(reloaded.status, "approved")

    def test_real_apply_writes_and_commits(self):
        proposal = self._make_proposal()
        review_proposal(proposal.proposal_id, "approved", proposals_dir=self.proposals_dir)
        result = apply_proposal(
            proposal.proposal_id, expected_sha256=proposal.proposed_sha256,
            dry_run=False, wiki_root=self.wiki_root, proposals_dir=self.proposals_dir,
        )
        self.assertFalse(result["dry_run"])
        self.assertEqual(
            self.project_md.read_text(),
            "# Project Atlas\nProject Atlas uses PostgreSQL and Redis.",
        )
        self.assertIsNotNone(result["commit_sha"])

        # the commit is real, in the real repo
        log = subprocess.run(
            ["git", "-C", str(self.wiki_root), "log", "--oneline", "-1"],
            check=True, capture_output=True, text=True,
        ).stdout
        self.assertIn(proposal.proposal_id, log)

        reloaded = get_proposal(proposal.proposal_id, proposals_dir=self.proposals_dir)
        self.assertEqual(reloaded.status, "applied")
        self.assertEqual(reloaded.applied_commit_sha, result["commit_sha"])

    def test_apply_refused_on_stale_base(self):
        """The target changed since the proposal was created — refused by name, not overwritten."""
        proposal = self._make_proposal()
        review_proposal(proposal.proposal_id, "approved", proposals_dir=self.proposals_dir)

        # simulate independent drift after the proposal was made
        self.project_md.write_text("# Project Atlas\nSomeone else changed this.", encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            apply_proposal(
                proposal.proposal_id, expected_sha256=proposal.proposed_sha256,
                dry_run=False, wiki_root=self.wiki_root, proposals_dir=self.proposals_dir,
            )
        self.assertIn("changed since", str(ctx.exception))
        self.assertEqual(self.project_md.read_text(), "# Project Atlas\nSomeone else changed this.")

    def test_create_operation_refused_if_target_now_exists(self):
        """A CREATE proposal whose target was created by something else in the meantime."""
        proposal = create_doc_proposal(
            target_path="WIKI/new-note.md",
            proposed_content="# New Note\nBrand new.",
            rationale="A new page.",
            wiki_root=self.wiki_root,
            proposals_dir=self.proposals_dir,
        )
        self.assertEqual(proposal.operation, "create")
        review_proposal(proposal.proposal_id, "approved", proposals_dir=self.proposals_dir)

        # someone/something else creates the file first
        target = self.wiki_root / "WIKI" / "new-note.md"
        target.write_text("# New Note\nSomeone beat us to it.", encoding="utf-8")

        with self.assertRaises(ValueError):
            apply_proposal(
                proposal.proposal_id, expected_sha256=proposal.proposed_sha256,
                dry_run=False, wiki_root=self.wiki_root, proposals_dir=self.proposals_dir,
            )
        self.assertEqual(target.read_text(), "# New Note\nSomeone beat us to it.")

    def test_create_operation_applies_cleanly(self):
        proposal = create_doc_proposal(
            target_path="WIKI/new-note.md",
            proposed_content="# New Note\nBrand new.",
            rationale="A new page.",
            wiki_root=self.wiki_root,
            proposals_dir=self.proposals_dir,
        )
        review_proposal(proposal.proposal_id, "approved", proposals_dir=self.proposals_dir)
        result = apply_proposal(
            proposal.proposal_id, expected_sha256=proposal.proposed_sha256,
            dry_run=False, wiki_root=self.wiki_root, proposals_dir=self.proposals_dir,
        )
        self.assertFalse(result["dry_run"])
        target = self.wiki_root / "WIKI" / "new-note.md"
        self.assertEqual(target.read_text(), "# New Note\nBrand new.")

    # -- bulk_reject_proposals -----------------------------------------------

    def test_bulk_reject_partial_success(self):
        p1 = self._make_proposal("# Project Atlas\nVariant one.")
        p2 = self._make_proposal("# Project Atlas\nVariant two.")
        p3 = self._make_proposal("# Project Atlas\nVariant three.")
        review_proposal(p3.proposal_id, "approved", proposals_dir=self.proposals_dir)  # already decided

        result = bulk_reject_proposals(
            [p1.proposal_id, p2.proposal_id, p3.proposal_id],
            reason="superseded",
            proposals_dir=self.proposals_dir,
        )
        self.assertEqual(sorted(result["rejected"]), sorted([p1.proposal_id, p2.proposal_id]))
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped"][0]["proposal_id"], p3.proposal_id)

        for pid in (p1.proposal_id, p2.proposal_id):
            self.assertEqual(get_proposal(pid, proposals_dir=self.proposals_dir).status, "rejected")
        # p3's prior approval is untouched by the bulk call
        self.assertEqual(get_proposal(p3.proposal_id, proposals_dir=self.proposals_dir).status, "approved")

    # -- list_proposals still works with the new fields ------------------

    def test_list_proposals_filters_by_new_statuses(self):
        p1 = self._make_proposal("# Project Atlas\nA.")
        p2 = self._make_proposal("# Project Atlas\nB.")
        review_proposal(p1.proposal_id, "approved", proposals_dir=self.proposals_dir)
        review_proposal(p2.proposal_id, "rejected", proposals_dir=self.proposals_dir)

        approved = list_proposals(proposals_dir=self.proposals_dir, status="approved")
        rejected = list_proposals(proposals_dir=self.proposals_dir, status="rejected")
        self.assertEqual([p.proposal_id for p in approved], [p1.proposal_id])
        self.assertEqual([p.proposal_id for p in rejected], [p2.proposal_id])

    def test_old_proposal_json_without_new_fields_still_loads(self):
        """Backward compatibility with the 76 pre-MS6d proposal files on disk."""
        proposal = self._make_proposal()
        # simulate a pre-MS6d file: strip the new keys entirely
        import json
        path = self.proposals_dir / f"{proposal.proposal_id}.json"
        data = json.loads(path.read_text())
        for key in ("reviewer", "reviewed_at", "review_notes", "applied_at", "applied_commit_sha"):
            data.pop(key, None)
        path.write_text(json.dumps(data), encoding="utf-8")

        reloaded = get_proposal(proposal.proposal_id, proposals_dir=self.proposals_dir)
        self.assertIsNotNone(reloaded)
        self.assertIsNone(reloaded.reviewer)
        self.assertEqual(reloaded.status, "pending_review")


class TestDestructiveUpdateGuard(unittest.TestCase):
    """2026-10-03: whole-file updates written from a page snippet would have
    deleted most of their page. Updates removing >30% of lines are refused."""

    def setUp(self):
        self._wiki = tempfile.TemporaryDirectory()
        self._props = tempfile.TemporaryDirectory()
        self.wiki_root = Path(self._wiki.name)
        self.proposals_dir = Path(self._props.name) / "doc-proposals"
        (self.wiki_root / "WIKI").mkdir()
        self.page = self.wiki_root / "WIKI" / "page.md"
        self.lines = [f"line {i} of the existing page" for i in range(20)]
        self.page.write_text("\n".join(self.lines) + "\n", encoding="utf-8")

    def tearDown(self):
        self._wiki.cleanup()
        self._props.cleanup()

    def _approved(self, content):
        p = create_doc_proposal(target_path="WIKI/page.md", proposed_content=content, rationale="r",
                                wiki_root=self.wiki_root, proposals_dir=self.proposals_dir)
        review_proposal(p.proposal_id, "approved", proposals_dir=self.proposals_dir)
        return p

    def _apply(self, p, **kw):
        return apply_proposal(p.proposal_id, expected_sha256=p.proposed_sha256, dry_run=False,
                              wiki_root=self.wiki_root, proposals_dir=self.proposals_dir, **kw)

    def test_truncating_rewrite_is_refused_even_on_dry_run(self):
        p = self._approved("\n".join(self.lines[:5]) + "\n")
        with self.assertRaises(ValueError) as ctx:
            apply_proposal(p.proposal_id, expected_sha256=p.proposed_sha256, dry_run=True,
                           wiki_root=self.wiki_root, proposals_dir=self.proposals_dir)
        self.assertIn("75%", str(ctx.exception))
        self.assertEqual(self.page.read_text().count("\n"), 20)

    def test_additive_update_passes(self):
        p = self._approved("\n".join(self.lines + ["## New", "new material"]) + "\n")
        self.assertFalse(self._apply(p)["dry_run"])
        self.assertIn("new material", self.page.read_text())

    def test_small_edit_passes(self):
        edited = list(self.lines)
        edited[3] = "line 3, corrected"
        p = self._approved("\n".join(edited) + "\n")
        self._apply(p)
        self.assertIn("line 3, corrected", self.page.read_text())

    def test_force_overrides(self):
        p = self._approved("# Rewritten\n")
        self._apply(p, force=True)
        self.assertEqual(self.page.read_text(), "# Rewritten\n")


class TestPostApplyStaleness(unittest.IsolatedAsyncioTestCase):
    """apply_doc_proposal must invalidate search_wiki's cache on a real apply
    (found 2026-09-16: a real apply's target was invisible to search_wiki until
    something else happened to force a rescan)."""

    def test_invalidate_corpus_cache_clears_engine_and_assets(self):
        from server.providers.wiki.scanner import _GLOBAL_CORPUS_MANAGER, invalidate_corpus_cache

        _GLOBAL_CORPUS_MANAGER._engine = object()
        _GLOBAL_CORPUS_MANAGER._assets = [object()]

        invalidate_corpus_cache()

        self.assertIsNone(_GLOBAL_CORPUS_MANAGER._engine)
        self.assertIsNone(_GLOBAL_CORPUS_MANAGER._assets)

    async def test_real_apply_invalidates_cache(self):
        from unittest.mock import patch
        from server.mcp import apply_doc_proposal as mcp_apply_doc_proposal

        fake_result = {
            "dry_run": False,
            "proposal_id": "prop_fake",
            "wrote": "WIKI/fake.md",
            "operation": "create",
            "commit_sha": "abc123",
        }
        with patch("server.mcp.apply_proposal", return_value=fake_result), \
             patch("server.mcp.invalidate_corpus_cache") as mock_invalidate:
            message = await mcp_apply_doc_proposal(
                proposal_id="prop_fake", expected_sha256="deadbeef", dry_run=False
            )

        mock_invalidate.assert_called_once()
        self.assertIn("cache was invalidated", message)

    async def test_dry_run_apply_does_not_invalidate_cache(self):
        from unittest.mock import patch
        from server.mcp import apply_doc_proposal as mcp_apply_doc_proposal

        fake_result = {
            "dry_run": True,
            "proposal_id": "prop_fake",
            "would_write": "WIKI/fake.md",
            "operation": "create",
        }
        with patch("server.mcp.apply_proposal", return_value=fake_result), \
             patch("server.mcp.invalidate_corpus_cache") as mock_invalidate:
            message = await mcp_apply_doc_proposal(
                proposal_id="prop_fake", expected_sha256="deadbeef", dry_run=True
            )

        mock_invalidate.assert_not_called()
        self.assertNotIn("cache was invalidated", message)


class TestProposalSubfolders(unittest.TestCase):
    """2026-09-18: doc-proposals/ moves a proposal into approved/ or
    rejected/ on a terminal verdict, instead of leaving every status flat
    in the root — found once the root grew to 78 undifferentiated files."""

    def setUp(self):
        self.temp_wiki = tempfile.TemporaryDirectory()
        self.wiki_root = Path(self.temp_wiki.name)
        self.temp_proposals = tempfile.TemporaryDirectory()
        self.proposals_dir = Path(self.temp_proposals.name) / "doc-proposals"

        wiki_dir = self.wiki_root / "WIKI"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        self.project_md = wiki_dir / "project.md"
        self.project_md.write_text("# Project Atlas\nUses PostgreSQL.", encoding="utf-8")

        _git("init", "-q", cwd=self.wiki_root)
        _git("config", "user.email", "test@example.com", cwd=self.wiki_root)
        _git("config", "user.name", "Test", cwd=self.wiki_root)
        _git("add", "-A", cwd=self.wiki_root)
        _git("commit", "-q", "-m", "initial", cwd=self.wiki_root)

    def tearDown(self):
        self.temp_wiki.cleanup()
        self.temp_proposals.cleanup()

    def _make_proposal(self):
        return create_doc_proposal(
            target_path="WIKI/project.md",
            proposed_content="# Project Atlas\nUses PostgreSQL and Redis.",
            rationale="Adding Redis.",
            wiki_root=self.wiki_root,
            proposals_dir=self.proposals_dir,
        )

    def test_new_proposal_lives_in_flat_root(self):
        prop = self._make_proposal()
        self.assertTrue((self.proposals_dir / f"{prop.proposal_id}.json").exists())
        self.assertFalse((self.proposals_dir / "approved" / f"{prop.proposal_id}.json").exists())
        self.assertFalse((self.proposals_dir / "rejected" / f"{prop.proposal_id}.json").exists())

    def test_approve_moves_to_approved_subfolder(self):
        prop = self._make_proposal()
        review_proposal(prop.proposal_id, "approved", proposals_dir=self.proposals_dir)

        self.assertFalse((self.proposals_dir / f"{prop.proposal_id}.json").exists())
        self.assertTrue((self.proposals_dir / "approved" / f"{prop.proposal_id}.json").exists())
        # get_proposal/list_proposals still find it despite the move
        self.assertEqual(get_proposal(prop.proposal_id, proposals_dir=self.proposals_dir).status, "approved")
        ids = {p.proposal_id for p in list_proposals(proposals_dir=self.proposals_dir)}
        self.assertIn(prop.proposal_id, ids)

    def test_reject_moves_to_rejected_subfolder(self):
        prop = self._make_proposal()
        review_proposal(prop.proposal_id, "rejected", proposals_dir=self.proposals_dir)

        self.assertFalse((self.proposals_dir / f"{prop.proposal_id}.json").exists())
        self.assertTrue((self.proposals_dir / "rejected" / f"{prop.proposal_id}.json").exists())
        self.assertEqual(get_proposal(prop.proposal_id, proposals_dir=self.proposals_dir).status, "rejected")

    def test_apply_stays_in_approved_subfolder(self):
        prop = self._make_proposal()
        review_proposal(prop.proposal_id, "approved", proposals_dir=self.proposals_dir)
        apply_proposal(
            prop.proposal_id, expected_sha256=prop.proposed_sha256, dry_run=False,
            wiki_root=self.wiki_root, proposals_dir=self.proposals_dir,
        )

        # applied is a sub-state of approved -- no third folder
        self.assertTrue((self.proposals_dir / "approved" / f"{prop.proposal_id}.json").exists())
        self.assertFalse((self.proposals_dir / "rejected" / f"{prop.proposal_id}.json").exists())
        self.assertEqual(get_proposal(prop.proposal_id, proposals_dir=self.proposals_dir).status, "applied")


if __name__ == "__main__":
    unittest.main()
