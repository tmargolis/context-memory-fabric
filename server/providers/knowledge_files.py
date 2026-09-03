"""FileKnowledgeProvider: the local-file durable-knowledge provider.

Wraps server.wiki/server.corpus behind server.core.protocols.KnowledgeProvider
rather than relocating their code (Milestone 1; see
docs/adr/0002-provider-boundaries.md). Unlike server/memory.py,
server.wiki/server.corpus have no Graphiti-shaped coupling problem to solve
by moving — they are already a self-contained local-filesystem
implementation with no external service dependency, and dozens of existing
tests import their classes and functions by name (CorpusScanner,
WikiCorpusManager, search_wiki, search_corpus, scan_corpus, etc.). This
provider is the seam a second knowledge provider (e.g. Milestone 5's GitHub
provider) implements alongside, without requiring wiki.py/corpus.py to
change.
"""

import os

from dotenv import load_dotenv

from server.corpus import SearchResult
from server.wiki import search_wiki


class FileKnowledgeProvider:
    """Thin class wrapper satisfying server.core.protocols.KnowledgeProvider."""

    def is_configured(self) -> bool:
        """Cheap presence check — does not touch the filesystem.

        Calls load_dotenv() itself (as server.corpus.get_corpus_root() does)
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
