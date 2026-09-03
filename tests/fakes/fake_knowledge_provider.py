"""A trivial in-memory KnowledgeProvider fake — see fake_memory_provider.py
for the rationale. Backs a fixed list of documents rather than a real
filesystem corpus, so tests do not depend on LLM_WIKI_PATH being set.
"""

from dataclasses import dataclass, field
from typing import Optional

from server.corpus import ExtractionStatus, MatchBasis, SearchResult


@dataclass
class FakeDocument:
    relative_path: str
    content: str
    top_level_area: str = "TEST"


class FakeKnowledgeProvider:
    """Structurally satisfies server.core.protocols.KnowledgeProvider."""

    def __init__(self, documents: Optional[list[FakeDocument]] = None, configured: bool = True) -> None:
        self._documents = documents or []
        self._configured = configured

    def is_configured(self) -> bool:
        return self._configured

    def search(
        self,
        query: str,
        max_results: int = 10,
        force_rescan: bool = False,
    ) -> list[SearchResult]:
        q = query.lower()
        results = []
        for doc in self._documents:
            if q in doc.content.lower() or q in doc.relative_path.lower():
                idx = doc.content.lower().find(q)
                snippet = doc.content[max(0, idx - 40): idx + 80] if idx >= 0 else doc.content[:80]
                results.append(
                    SearchResult(
                        relative_path=doc.relative_path,
                        top_level_area=doc.top_level_area,
                        media_type="text/markdown",
                        match_basis=MatchBasis.CONTENT.value,
                        extraction_status=ExtractionStatus.EXTRACTED.value,
                        matched_snippet=snippet,
                        relevance_score=1.0,
                    )
                )
        return results[:max_results]
