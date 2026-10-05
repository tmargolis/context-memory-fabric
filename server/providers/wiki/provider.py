"""FileKnowledgeProvider: the local-file durable-knowledge provider.

Wraps server.providers.wiki.scanner/server.providers.wiki.corpus behind
server.core.protocols.KnowledgeProvider. Originally (Milestone 1; see
docs/adr/0002-provider-boundaries.md) this wrapped corpus.py/wiki.py/
extractors.py in place at the server/ root rather than relocating their
code -- they had no Graphiti-shaped coupling problem to solve by moving,
unlike server/memory.py. They've since moved anyway (docs/plan-active.md,
"Wiki→doc rename", 2026-09-19), grouped into this same server/providers/wiki/
package alongside this file, once the user wanted providers organized into
per-provider subfolders rather than scattered at server/ root -- dozens of
existing tests still import their classes and functions by name
(CorpusScanner, WikiCorpusManager, search_wiki, search_corpus, scan_corpus,
etc.), just from their new module paths. This provider is the seam a
second knowledge provider (e.g. Milestone 5's GitHub provider) implements
alongside, in its own server/providers/<name>/ package.
"""

from datetime import datetime, timezone
import os
from typing import Any

from dotenv import load_dotenv

from server.core.models import KnowledgeResult
from server.providers.wiki.corpus import SearchResult, get_corpus_root
from server.providers.wiki.scanner import search_wiki


class FileKnowledgeProvider:
    """Thin class wrapper satisfying server.core.protocols.KnowledgeProvider,
    and (MS5) server.core.protocols.KnowledgeSource via name/query()."""

    name = "wiki"

    def is_configured(self) -> bool:
        """Cheap presence check — does not touch the filesystem.

        Calls load_dotenv() itself (as server.providers.wiki.corpus.get_corpus_root() does)
        rather than relying on some other module having already loaded .env
        as an import-time side effect — that dependency existed only
        incidentally (server.memory's import chain happens to call
        load_dotenv() first in the current server.mcp import order) and
        would silently break if that order ever changed.

        Matches server.core.config.CMFConfig.knowledge_enabled's check;
        kept as a duplicate one-line check here (rather than importing
        CMFConfig) so this provider has no dependency on server.core.config,
        consistent with providers depending on core.protocols/core.errors
        but not on each other's configuration surface.
        """
        load_dotenv()
        raw = os.getenv("LLM_WIKI_PATH")
        return bool(raw and raw.strip())

    def search(
        self,
        query: str,
        max_results: int = 10,
        force_rescan: bool = False,
    ) -> list[SearchResult]:
        # search_wiki(format_for_mcp=False) and search_corpus (the function
        # server.context used pre-Milestone-1) both resolve to the same
        # cached _GLOBAL_CORPUS_MANAGER for the default root_path=None case,
        # so this is behavior-identical when force_rescan=False. Calling
        # search_wiki here (rather than search_corpus) is deliberate: it is
        # the one entry point that actually exposes force_rescan.
        return search_wiki(query, max_results=max_results, force_rescan=force_rescan, format_for_mcp=False)

    def query(self, text: str, max_results: int = 10) -> list[KnowledgeResult]:
        """KnowledgeSource view of search(): the same hits as KnowledgeResult.

        document_id is the corpus-relative path; source_version is the file's
        mtime when it was read, so two reads of a changed note are told apart.
        """
        if not text.strip():
            return []
        root = get_corpus_root()
        out = []
        for r in self.search(text, max_results=max_results):
            path = root / r.relative_path
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                mtime = None
            out.append(KnowledgeResult(
                provider=self.name,
                document_id=r.relative_path,
                title=r.filename or r.relative_path,
                excerpt=r.matched_snippet or "",
                uri=path.as_uri() if path.is_absolute() else None,
                source_timestamp=mtime,
                retrieval_score=r.relevance_score,
                retrieval_method=r.match_basis,
                scope="local:llm-wiki",
                metadata={"area": r.top_level_area, "media_type": r.media_type,
                          "extraction_status": r.extraction_status},
                source_version=mtime.isoformat() if mtime else None,
            ))
        return out

    def propose_change(self, **proposal: Any) -> dict[str, Any]:
        """Stage a reviewable doc proposal (never edits the wiki): propose_doc_update's path."""
        from server.proposals import create_doc_proposal

        prop = create_doc_proposal(**proposal)
        return {"status": "proposed", "provider": self.name, "proposal_id": prop.proposal_id,
                "target_path": prop.target_path}
