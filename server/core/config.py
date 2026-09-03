"""Centralized configuration and capability discovery (Milestone 1).

Existing modules (server.corpus.get_corpus_root, server.memory's Graphiti
setup, server.proposals) each read their own environment variables directly
and remain the source of truth for their own validated values — this module
does not replace that validation. What it adds is a single, cheap place to
answer "is a given capability configured at all", so server.mcp and
server.context can make registration/degradation decisions without
importing provider-specific modules or triggering their (sometimes
expensive, sometimes side-effecting) validation just to ask the question.

Deliberately not a cached singleton: environment variables can change
between calls in tests (see tests/conftest.py's FALKORDB_DATABASE override),
and re-reading os.environ is cheap enough that caching would only risk
staleness for no measurable benefit.
"""

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


@dataclass(frozen=True)
class CMFConfig:
    """A snapshot of capability-relevant configuration, read fresh from the
    environment. Individual fields are informational; validation of a
    configured value (e.g. that LLM_WIKI_PATH points at a real, readable
    directory) stays with the module that owns that capability.
    """

    llm_wiki_path: Optional[str]
    gemini_api_key: Optional[str]
    falkordb_host: str
    falkordb_port: int
    falkordb_password: Optional[str]
    falkordb_database: Optional[str]
    cmf_state_dir: Path

    @property
    def knowledge_enabled(self) -> bool:
        """True if a local-file knowledge provider is configured at all.

        Does not validate the path exists/is readable — that check runs
        lazily on first real access (server.corpus.get_corpus_root), exactly
        as it did before Milestone 1. This property only answers "should
        server.mcp register search_wiki/propose_wiki_update", which must be
        cheap and side-effect-free since it runs at every server startup.
        """
        return bool(self.llm_wiki_path and self.llm_wiki_path.strip())

    @property
    def memory_enabled(self) -> bool:
        """True if the Graphiti/FalkorDB memory provider has its two
        required values present. Today this is always required (there is no
        memory-disabled deployment mode yet — see ROADMAP.md's "No required
        memory backend" principle, still aspirational for the memory side).
        Exposed now so server.mcp can assert a clear startup error instead
        of the tool-call-time RuntimeError raised by create_graphiti today.
        """
        return bool(self.gemini_api_key) and bool(
            self.falkordb_database and self.falkordb_database.strip()
        )


def _resolve_state_dir() -> Path:
    """Mirror server.proposals.get_proposals_dir()'s resolution exactly.

    That function remains the actual source of truth (it also creates the
    directory); this duplicates only the path computation, read-only, so
    capability-checking code can report where proposals will land without
    the create-directory side effect. If the two ever diverge, trust
    server.proposals.get_proposals_dir() — file a fix here to match it.
    """
    env_state_dir = os.getenv("CMF_STATE_DIR")
    if env_state_dir:
        return Path(env_state_dir).expanduser().resolve() / "wiki-proposals"
    project_root = Path(__file__).resolve().parent.parent.parent
    return project_root / "wiki-proposals"


def load_config() -> CMFConfig:
    """Read the current environment into a CMFConfig snapshot.

    Calls load_dotenv() itself rather than depending on some other module
    having already loaded .env as an import-time side effect (see
    server.providers.knowledge_files.FileKnowledgeProvider.is_configured
    for why that dependency is fragile).
    """
    load_dotenv()
    return CMFConfig(
        llm_wiki_path=os.getenv("LLM_WIKI_PATH"),
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        falkordb_host=os.getenv("FALKORDB_HOST", "localhost"),
        falkordb_port=int(os.getenv("FALKORDB_PORT", "6379")),
        falkordb_password=os.getenv("FALKORDB_PASSWORD") or None,
        falkordb_database=os.getenv("FALKORDB_DATABASE"),
        cmf_state_dir=_resolve_state_dir(),
    )
