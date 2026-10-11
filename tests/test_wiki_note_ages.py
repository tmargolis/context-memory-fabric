"""B03: get_context shows each wiki hit's age (created / updated)."""

import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from server.context import _wiki_ages, get_context
from server.providers.wiki.corpus import SearchResult
from server.providers.wiki.provider import FileKnowledgeProvider
from tests.fakes.fake_knowledge_provider import FakeDocument, FakeKnowledgeProvider
from tests.fakes.fake_memory_provider import FakeMemoryProvider


def _hit(path: str) -> SearchResult:
    return SearchResult(source="durable_knowledge", relative_path=path, filename=Path(path).name,
                        top_level_area="WIKI", media_type="text/markdown", extractor="file",
                        extraction_status="extracted", matched_snippet="Atlas notes.",
                        match_basis="content", relevance_score=1.0)


class _StubFileWiki(FileKnowledgeProvider):
    def is_configured(self) -> bool:
        return True

    def search(self, query, max_results=10, force_rescan=False):
        return [_hit("WIKI/Atlas.md")]


class TestWikiAges(unittest.TestCase):
    def test_ages_from_a_git_wiki(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q"], check=True)
            env = {**os.environ, "GIT_AUTHOR_DATE": "2026-09-01T10:00:00Z", "GIT_COMMITTER_DATE": "2026-09-01T10:00:00Z",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
            (root / "seed.md").write_text("first commit\n")
            subprocess.run(["git", "-C", tmp, "add", "-A"], check=True, env=env)
            subprocess.run(["git", "-C", tmp, "commit", "-qm", "init"], check=True, env=env)
            env.update(GIT_AUTHOR_DATE="2026-10-05T10:00:00Z", GIT_COMMITTER_DATE="2026-10-05T10:00:00Z")
            (root / "WIKI").mkdir()
            (root / "WIKI/Atlas.md").write_text("atlas\n")
            subprocess.run(["git", "-C", tmp, "add", "-A"], check=True, env=env)
            subprocess.run(["git", "-C", tmp, "commit", "-qm", "atlas"], check=True, env=env)

            now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
            ages = _wiki_ages(["WIKI/Atlas.md", "WIKI/missing.md"], root=root, now=now)
            self.assertEqual(ages, {"WIKI/Atlas.md": "created 2026-10-05 | updated 2026-10-05 (5 days ago)"})

    def test_no_wiki_means_no_ages(self):
        with patch("server.context.get_corpus_root", side_effect=RuntimeError("LLM_WIKI_PATH unset")):
            self.assertEqual(_wiki_ages(["WIKI/Atlas.md"]), {})


class TestGetContextAgeLine(unittest.IsolatedAsyncioTestCase):
    async def _context(self, knowledge):
        with patch("server.retrieval_expansion.expand_episodes_to_notes", return_value=[]), \
             patch("server.retrieval_expansion.expand_notes_to_episodes", return_value=[]):
            return await get_context("Atlas", knowledge_provider=knowledge, memory_provider=FakeMemoryProvider(),
                                     knowledge_sources=[])

    async def test_file_wiki_hits_get_an_age_line(self):
        with patch("server.context._wiki_ages", return_value={"WIKI/Atlas.md": "created 2026-04-22 (on or before)"}) as ages:
            ctx = await self._context(_StubFileWiki())
        ages.assert_called_once_with(["WIKI/Atlas.md"])
        self.assertIn("- **Age:** created 2026-04-22 (on or before)", ctx)

    async def test_other_providers_get_no_age_line(self):
        knowledge = FakeKnowledgeProvider(documents=[FakeDocument("WIKI/Atlas.md", "Atlas notes.")])
        with patch("server.context._wiki_ages") as ages:
            ctx = await self._context(knowledge)
        ages.assert_not_called()
        self.assertNotIn("**Age:**", ctx)


if __name__ == "__main__":
    unittest.main()
