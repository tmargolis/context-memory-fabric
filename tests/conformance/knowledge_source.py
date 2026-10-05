"""KnowledgeSource conformance (MS5 exit gate).

Mix `KnowledgeSourceConformance` into a unittest.TestCase and implement:

    make_source()            -> a configured source over a small fixture corpus
    make_unconfigured_source() -> the same class with no configuration
    known_query              -> a query the fixture corpus answers
    fingerprint()            -> a value that changes if the fixture corpus changes

Every provider runs the same checks, so "a second provider passes
conformance without changing core" is a statement about this file, not
about hand-picked tests per provider.
"""

from __future__ import annotations

from server.core.models import KnowledgeResult
from server.core.protocols import KnowledgeSource
from server.knowledge import search_knowledge


class KnowledgeSourceConformance:
    known_query: str = ""

    def make_source(self):  # pragma: no cover - supplied by the concrete test
        raise NotImplementedError

    def make_unconfigured_source(self):  # pragma: no cover
        raise NotImplementedError

    def fingerprint(self):  # pragma: no cover
        raise NotImplementedError

    # --- identity ---
    def test_implements_protocol_with_stable_name(self):
        a, b = self.make_source(), self.make_source()
        self.assertIsInstance(a, KnowledgeSource)
        self.assertIsInstance(a.name, str)
        self.assertTrue(a.name.strip())
        self.assertEqual(a.name, b.name)

    def test_is_configured_is_a_cheap_bool(self):
        self.assertIs(self.make_source().is_configured(), True)
        self.assertIs(self.make_unconfigured_source().is_configured(), False)

    # --- query contract ---
    def test_known_query_returns_attributed_results(self):
        source = self.make_source()
        results = source.query(self.known_query, max_results=10)
        self.assertTrue(results, "the fixture corpus should answer known_query")
        for r in results:
            self.assertIsInstance(r, KnowledgeResult)
            self.assertEqual(r.provider, source.name)
            self.assertTrue(r.document_id)
            self.assertTrue(r.title)
            self.assertTrue(r.scope, "every result names its access scope")
            self.assertTrue(r.source_version or r.source_timestamp, "every result says what version was read")
        ids = [r.document_id for r in results]
        self.assertEqual(len(ids), len(set(ids)), "no duplicate documents within one provider")

    def test_max_results_is_respected(self):
        self.assertLessEqual(len(self.make_source().query(self.known_query, max_results=1)), 1)

    def test_scores_are_best_first_when_present(self):
        scores = [r.retrieval_score for r in self.make_source().query(self.known_query, max_results=10)
                  if r.retrieval_score is not None]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_document_ids_are_stable(self):
        a = [r.document_id for r in self.make_source().query(self.known_query, max_results=10)]
        b = [r.document_id for r in self.make_source().query(self.known_query, max_results=10)]
        self.assertEqual(a, b)

    def test_blank_query_returns_nothing(self):
        for q in ("", "   "):
            self.assertEqual(self.make_source().query(q, max_results=5), [])

    def test_odd_queries_do_not_raise(self):
        source = self.make_source()
        for q in ("Kovács — café", "(unbalanced [brackets", "*", "a_b | c", "x" * 500):
            self.assertIsInstance(source.query(q, max_results=3), list)

    def test_query_is_read_only(self):
        before = self.fingerprint()
        self.make_source().query(self.known_query, max_results=10)
        self.assertEqual(self.fingerprint(), before)

    # --- behaves inside the core fan-out ---
    def test_participates_in_search_knowledge(self):
        source = self.make_source()
        search = search_knowledge(self.known_query, max_results_per_provider=3, sources=[source])
        self.assertIn(source.name, search.by_provider)
        self.assertTrue(all(r.provider == source.name for r in search.results))
        unconfigured = self.make_unconfigured_source()
        search = search_knowledge(self.known_query, sources=[unconfigured])
        self.assertEqual(search.not_configured, [unconfigured.name])
        self.assertEqual(search.results, [])
