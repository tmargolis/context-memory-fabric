"""MS5 — provider-neutral knowledge core: registry, fan-out, attribution, get_context.

Also runs the wiki provider through the shared conformance suite against a
throwaway corpus (LLM_WIKI_PATH pointed at a temp dir, corpus cache reset).
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from server.context import get_context
from server.core.models import KnowledgeResult
from server.knowledge import (
    format_knowledge_search,
    load_knowledge_sources,
    propose_knowledge_change,
    search_knowledge,
)
from server.providers.wiki.provider import FileKnowledgeProvider
from server.providers.wiki.scanner import invalidate_corpus_cache
from tests.conformance.knowledge_source import KnowledgeSourceConformance
from tests.fakes.fake_knowledge_provider import FakeKnowledgeProvider
from tests.fakes.fake_memory_provider import FakeMemoryProvider

T = datetime(2026, 9, 1, tzinfo=timezone.utc)


class StubSource:
    """A minimal KnowledgeSource with canned results."""

    def __init__(self, name, docs, configured=True, fail=False):
        self.name, self.docs, self.configured, self.fail = name, docs, configured, fail

    def is_configured(self):
        return self.configured

    def query(self, text, max_results=10):
        if self.fail:
            raise RuntimeError(f"{self.name} is down")
        return [
            KnowledgeResult(provider=self.name, document_id=f"{self.name}:{i}", title=t, excerpt=body,
                            scope=f"test:{self.name}", source_timestamp=T, retrieval_score=1.0 / (i + 1))
            for i, (t, body) in enumerate(self.docs) if text.lower() in body.lower()
        ][:max_results]


class TestFanOut(unittest.TestCase):
    def test_conflicting_sources_stay_separately_attributed(self):
        wiki = StubSource("wiki", [("Backend", "The journal backend is SQLite.")])
        mail = StubSource("gmail", [("Re: backend", "We moved the journal backend to Postgres.")])
        search = search_knowledge("journal backend", sources=[wiki, mail])
        self.assertEqual([r.provider for r in search.results], ["wiki", "gmail"])
        self.assertIn("SQLite", search.results[0].excerpt)
        self.assertIn("Postgres", search.results[1].excerpt)
        text = format_knowledge_search("journal backend", search)
        self.assertIn("[wiki] Backend", text)
        self.assertIn("[gmail] Re: backend", text)

    def test_interleaves_by_rank_without_cross_provider_scoring(self):
        a = StubSource("a", [(f"a{i}", "x") for i in range(3)])
        b = StubSource("b", [("b0", "x")])
        self.assertEqual([r.title for r in search_knowledge("x", sources=[a, b]).results], ["a0", "b0", "a1", "a2"])

    def test_same_document_text_in_two_providers_is_not_collapsed(self):
        a, b = StubSource("a", [("t", "same words")]), StubSource("b", [("t", "same words")])
        self.assertEqual(len(search_knowledge("same", sources=[a, b]).results), 2)

    def test_failing_and_unconfigured_sources_are_reported_not_fatal(self):
        ok = StubSource("ok", [("t", "hit")])
        search = search_knowledge("hit", sources=[ok, StubSource("down", [], fail=True), StubSource("off", [], configured=False)])
        self.assertEqual([r.provider for r in search.results], ["ok"])
        self.assertIn("down", search.errors)
        self.assertEqual(search.not_configured, ["off"])

    def test_provider_filter(self):
        a, b = StubSource("a", [("t", "x")]), StubSource("b", [("t", "x")])
        self.assertEqual({r.provider for r in search_knowledge("x", providers=["b"], sources=[a, b]).results}, {"b"})


class TestRegistry(unittest.TestCase):
    def test_default_is_the_wiki(self):
        with mock.patch.dict(os.environ, {"CMF_KNOWLEDGE_PROVIDERS": ""}):
            self.assertEqual([s.name for s in load_knowledge_sources()], ["wiki"])

    def test_loads_module_class_paths(self):
        spec = "server.providers.wiki.provider:FileKnowledgeProvider"
        self.assertIsInstance(load_knowledge_sources(spec)[0], FileKnowledgeProvider)

    def test_rejects_bad_specs(self):
        for spec in ("not_a_path", "server.knowledge:KnowledgeSearch",
                     "server.providers.wiki.provider:FileKnowledgeProvider,server.providers.wiki.provider:FileKnowledgeProvider"):
            with self.subTest(spec=spec), self.assertRaises((ValueError, TypeError)):
                load_knowledge_sources(spec)


class TestProposals(unittest.TestCase):
    def test_read_only_provider_says_so(self):
        result = propose_knowledge_change("gmail", sources=[StubSource("gmail", [])], target_path="x")
        self.assertEqual(result["status"], "unsupported")

    def test_unknown_provider(self):
        self.assertEqual(propose_knowledge_change("nope", sources=[])["status"], "error")


class TestGetContextOtherProviders(unittest.IsolatedAsyncioTestCase):
    async def test_other_provider_gets_its_own_attributed_section(self):
        mail = StubSource("gmail", [("Re: EV charger", "Approved the EV charger install for the garage.")])
        text = await get_context(
            "EV charger", knowledge_provider=FakeKnowledgeProvider([]), memory_provider=FakeMemoryProvider(),
            knowledge_sources=[FileKnowledgeProvider(), mail],
        )
        self.assertIn("KNOWLEDGE (Source: `gmail`)", text)
        self.assertIn("[gmail] Re: EV charger", text)
        self.assertEqual(text.count("KNOWLEDGE (Source: `wiki`)"), 0)  # the wiki keeps its own section

    async def test_no_other_providers_adds_nothing(self):
        text = await get_context("anything", knowledge_provider=FakeKnowledgeProvider([]),
                                 memory_provider=FakeMemoryProvider(), knowledge_sources=[FileKnowledgeProvider()])
        self.assertNotIn("📨 KNOWLEDGE", text)
        self.assertNotIn("Other knowledge providers", text)


class TestWikiConformance(KnowledgeSourceConformance, unittest.TestCase):
    known_query = "espresso"

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        (root / "WIKI").mkdir()
        (root / "WIKI" / "coffee.md").write_text("# Coffee\nDial in the espresso grinder before the grind change.\n")
        (root / "WIKI" / "espresso-machine.md").write_text("# Machine\nThe espresso machine descales monthly.\n")
        (root / "WIKI" / "tea.md").write_text("# Tea\nGreen tea at 80C.\n")
        cls._env = mock.patch.dict(os.environ, {"LLM_WIKI_PATH": str(root)})
        cls._env.start()
        invalidate_corpus_cache()

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()
        invalidate_corpus_cache()
        cls._tmp.cleanup()

    def make_source(self):
        return FileKnowledgeProvider()

    def make_unconfigured_source(self):
        source = FileKnowledgeProvider()
        source.is_configured = lambda: False
        return source

    def fingerprint(self):
        root = Path(self._tmp.name)
        return hashlib.sha256(b"".join(p.read_bytes() for p in sorted(root.rglob("*.md")))).hexdigest()


if __name__ == "__main__":
    unittest.main()
