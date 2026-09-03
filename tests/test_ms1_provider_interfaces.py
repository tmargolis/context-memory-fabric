"""Milestone 1 verification: provider protocols, capability discovery, and
zero-dependency test fakes.

See IMPLEMENTATION-PLAN.md's Milestone 1 section for the acceptance tests
and exit gate this file exists to satisfy:

- exit gate: "Can a second memory provider be stubbed against
  MemoryProvider without changing core?" -> TestFakeProvidersSatisfyProtocols
  + TestGetContextWithFakeProviders (the fakes ARE the second providers).
- acceptance test 3: "Majority of the suite runs against fakes with no
  FalkorDB and no Gemini key." -> TestGetContextWithFakeProviders makes no
  network call and needs neither FALKORDB_* nor GEMINI_API_KEY.
- acceptance test 1: server starts and passes tool-listing with
  LLM_WIKI_PATH unset; memory tools work, knowledge tools are absent.
  -> TestCapabilityDrivenToolRegistration.
"""

import asyncio
import unittest

from server.context import get_context
from server.core.protocols import KnowledgeProvider, MemoryProvider
from server.providers.knowledge_files import FileKnowledgeProvider
from server.providers.memory_graphiti import GraphitiMemoryProvider
from tests.fakes.fake_knowledge_provider import FakeDocument, FakeKnowledgeProvider
from tests.fakes.fake_memory_provider import FakeMemoryProvider


class TestFakeProvidersSatisfyProtocols(unittest.TestCase):
    """Milestone 1 exit gate: a trivial in-memory provider must structurally
    satisfy MemoryProvider/KnowledgeProvider with zero changes to core.
    """

    def test_fake_memory_provider_satisfies_protocol(self):
        self.assertIsInstance(FakeMemoryProvider(), MemoryProvider)

    def test_fake_knowledge_provider_satisfies_protocol(self):
        self.assertIsInstance(FakeKnowledgeProvider(), KnowledgeProvider)

    def test_real_providers_still_satisfy_their_protocols(self):
        # Guards against a future protocol change that accidentally
        # narrows the interface to only what the fakes happen to implement.
        self.assertIsInstance(GraphitiMemoryProvider(), MemoryProvider)
        self.assertIsInstance(FileKnowledgeProvider(), KnowledgeProvider)


class TestGetContextWithFakeProviders(unittest.IsolatedAsyncioTestCase):
    """get_context() exercised end-to-end with zero live dependencies:
    no FalkorDB, no GEMINI_API_KEY, no configured LLM_WIKI_PATH.
    """

    async def test_get_context_combines_fake_knowledge_and_memory(self):
        knowledge = FakeKnowledgeProvider(
            documents=[FakeDocument("WIKI/projects/Atlas.md", "Project Atlas uses PostgreSQL 16.")]
        )
        memory = FakeMemoryProvider()
        await memory.remember(
            content="On 2026-01-01, Project Atlas switched from MySQL to PostgreSQL 16.",
            name="atlas_db_decision",
        )

        result = await get_context(
            "Project Atlas",
            knowledge_provider=knowledge,
            memory_provider=memory,
        )

        self.assertIn("# Context Fabric: 'Project Atlas'", result)
        self.assertIn("PostgreSQL", result)
        self.assertIn("DURABLE KNOWLEDGE", result)
        self.assertIn("EPISODIC MEMORY", result)

    async def test_get_context_degrades_when_knowledge_unconfigured(self):
        knowledge = FakeKnowledgeProvider(configured=False)
        memory = FakeMemoryProvider()
        await memory.remember(content="Memory-only fact about Project Orion.", name="orion_fact")

        result = await get_context(
            "Project Orion",
            knowledge_provider=knowledge,
            memory_provider=memory,
        )

        self.assertIn("No knowledge provider configured", result)
        self.assertIn("Memory-only fact about Project Orion", result)

    async def test_get_context_reports_no_results_for_unmatched_topic(self):
        result = await get_context(
            "a topic nothing matches",
            knowledge_provider=FakeKnowledgeProvider(),
            memory_provider=FakeMemoryProvider(),
        )
        self.assertIn("No matching durable knowledge found", result)
        self.assertIn("No episodic memories found", result)

    async def test_empty_topic_short_circuits_without_calling_providers(self):
        class ExplodingProvider(FakeMemoryProvider):
            async def recall(self, query, max_results=10):
                raise AssertionError("recall() should not be called for an empty topic")

        result = await get_context(
            "   ",
            knowledge_provider=FakeKnowledgeProvider(),
            memory_provider=ExplodingProvider(),
        )
        self.assertIn("non-empty topic", result)


class TestCapabilityDrivenToolRegistration(unittest.IsolatedAsyncioTestCase):
    """server.mcp registers search_wiki/propose_wiki_update only when a
    knowledge provider is configured (acceptance test 1). The current
    process has LLM_WIKI_PATH set (this deployment's .env), so this test
    verifies the "configured" branch directly; the "unset" branch was
    verified interactively during Milestone 1 implementation (see
    IMPLEMENTATION-PLAN.md) since server.mcp reads capability once at
    import time and re-importing it mid-suite would not reflect a real
    process restart.
    """

    async def test_knowledge_tools_registered_when_llm_wiki_path_set(self):
        from server.mcp import app

        tools = await app.list_tools()
        names = {t.name for t in tools}
        self.assertIn("search_wiki", names)
        self.assertIn("propose_wiki_update", names)
        self.assertEqual(len(names), 9)


if __name__ == "__main__":
    unittest.main()
