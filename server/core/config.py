"""Centralized configuration and capability discovery (Milestone 1).

Existing modules (server.providers.wiki.corpus.get_corpus_root, server.memory's Graphiti
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

GEMINI_PROVIDER = "gemini"
LOCAL_PROVIDER = "local"
VALID_PROVIDERS = (GEMINI_PROVIDER, LOCAL_PROVIDER)

# MS4e: which entity-extraction profile add_episode() runs under (see
# server.providers.extraction_profile). "legacy" is the pre-MS4e behaviour
# and stays the default until the MS4e exit gate picks a replacement.
LEGACY_EXTRACTION = "legacy"
SELECTIVE_EXTRACTION = "selective"
TYPED_EXTRACTION = "typed"
TYPED_RECALL_EXTRACTION = "typed-recall"
VALID_EXTRACTION_PROFILES = (LEGACY_EXTRACTION, SELECTIVE_EXTRACTION, TYPED_EXTRACTION, TYPED_RECALL_EXTRACTION)

# Defaults describe the *current* deployment, not the target one: provider
# stays "gemini" so importing this module can never silently re-point a
# running server at a backend whose code path (Phase 2) does not exist yet.
# Flipping to local is a deliberate .env edit, made together with
# FALKORDB_DATABASE and EMBEDDING_DIM — see docs/SPARK-MIGRATION-PLAN.md Phase 6.
DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:12345/v1"
DEFAULT_LOCAL_API_KEY = "lm-studio"  # LM Studio ignores it; the OpenAI SDK requires non-empty
DEFAULT_LOCAL_LLM_MODEL = "zai-org/glm-4.7-flash"
DEFAULT_LOCAL_EMBED_MODEL = "text-embedding-nomic-embed-text-v1.5"

# "json_schema" (Mode A) after end-to-end testing against graphiti's real
# prompts. An earlier draft defaulted to "json_object" (Mode B: schema in the
# prompt, wire format rewritten to "text") on the strength of hand-written
# probe prompts. That does not survive contact with graphiti: given its
# extract_nodes prompt, GLM-4.7-Flash returns the *schema itself* --
# `{"$defs": {...}, "type": "object"}` -- and pydantic rejects it with
# "extracted_entities Field required".
#
# Constrained decoding cannot fail that way: the grammar makes returning the
# schema structurally impossible. Its own failure mode -- GLM and Qwen leaving
# `content` empty with the answer in `reasoning_content` -- is handled by
# server.providers.lmstudio_client, which is therefore required, not optional.
# Verified: a full add_episode + search round-trip on the Spark.
#
# The cost of this choice is Gemma-4-26B-A4B, whose control-token leak is
# triggered by constrained decoding specifically. Mode B stays selectable for
# a non-reasoning model that might prefer it.
DEFAULT_LOCAL_STRUCTURED_MODE = "json_schema"

# Must match the embedder's real output width. gemini-embedding-001 is
# requested at 1024; nomic-embed-text-v1.5 returns 768 and cannot be widened.
# Read here for reporting only — the value that actually reaches graphiti_core
# is the process environment, populated by server/__init__.py before
# graphiti_core is imported (see that module for why .env alone is too late).
DEFAULT_EMBEDDING_DIM = 1024


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

    # Spark-local inference (docs/SPARK-MIGRATION-PLAN.md Phase 1). Provider
    # selection is split in two deliberately: extraction and embedding have
    # very different cost/quality profiles, and the migration's fallback
    # position is the hybrid — local embeddings (high volume, low judgment)
    # with Gemini extraction (low volume, high judgment). Keeping one
    # variable for both would make that a code change instead of a config
    # change.
    llm_provider: str
    embed_provider: str
    local_base_url: str
    local_api_key: str
    local_llm_model: str
    local_embed_model: str
    local_structured_mode: str
    embedding_dim: int
    extraction_profile: str = LEGACY_EXTRACTION

    @property
    def llm_is_local(self) -> bool:
        return self.llm_provider == LOCAL_PROVIDER

    @property
    def embed_is_local(self) -> bool:
        return self.embed_provider == LOCAL_PROVIDER

    @property
    def knowledge_enabled(self) -> bool:
        """True if a local-file knowledge provider is configured at all.

        Does not validate the path exists/is readable — that check runs
        lazily on first real access (server.providers.wiki.corpus.get_corpus_root), exactly
        as it did before Milestone 1. This property only answers "should
        server.mcp register search_wiki/propose_doc_update", which must be
        cheap and side-effect-free since it runs at every server startup.
        """
        return bool(self.llm_wiki_path and self.llm_wiki_path.strip())

    @property
    def memory_enabled(self) -> bool:
        """True if the Graphiti/FalkorDB memory provider has everything it
        needs for the *configured* providers. Today memory is always
        required (there is no memory-disabled deployment mode yet — see
        ROADMAP.md's "No required memory backend" principle, still
        aspirational for the memory side). Exposed so server.mcp can assert
        a clear startup error instead of the tool-call-time RuntimeError
        raised by create_graphiti today.

        The credential check follows the two provider switches rather than
        assuming Gemini. A fully local deployment has no GEMINI_API_KEY at
        all, and the previous unconditional `bool(self.gemini_api_key)`
        would have reported memory as disabled on an otherwise perfectly
        configured box. Both switches are consulted because the hybrid
        (one local, one Gemini) is a supported configuration, and it needs
        the credentials of *both* sides.
        """
        if not (self.falkordb_database and self.falkordb_database.strip()):
            return False

        providers = (self.llm_provider, self.embed_provider)
        if GEMINI_PROVIDER in providers and not self.gemini_api_key:
            return False
        if LOCAL_PROVIDER in providers and not (self.local_base_url and self.local_base_url.strip()):
            return False
        return True


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
        return Path(env_state_dir).expanduser().resolve() / "doc-proposals"
    project_root = Path(__file__).resolve().parent.parent.parent
    return project_root / "doc-proposals"


def _provider(env_var: str, default: str = GEMINI_PROVIDER) -> str:
    """Read a provider switch, rejecting anything not recognised.

    Failing loudly matters more here than tolerating a typo: silently
    treating `CMF_LLM_PROVIDER=lcoal` as "gemini" would send extraction
    traffic to a metered API the operator believed they had switched away
    from, and the only symptom would be quota consumption.
    """
    raw = os.getenv(env_var)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value not in VALID_PROVIDERS:
        raise ValueError(
            f"{env_var}={raw!r} is not a recognised provider. "
            f"Valid values are {', '.join(VALID_PROVIDERS)}."
        )
    return value


def capture_llm_provider_from_env() -> str:
    """Read CMF_CAPTURE_LLM_PROVIDER: the LLM provider unattended background
    capture workers extract with, independent of CMF_LLM_PROVIDER.

    Defaults to "local": a poller runs with no human review gate in front of
    it, and one long transcript is dozens of extraction calls, so it must
    never fall through to a metered API unless the operator opts in.
    """
    return _provider("CMF_CAPTURE_LLM_PROVIDER", default=LOCAL_PROVIDER)


def extraction_profile_from_env() -> str:
    """Read CMF_EXTRACTION_PROFILE, rejecting anything not recognised.

    Same fail-loudly reasoning as _provider(): a typo silently falling back
    to "legacy" would make an A/B replay measure the wrong thing, and the
    only symptom would be a graph that looks exactly like the baseline.
    """
    raw = os.getenv("CMF_EXTRACTION_PROFILE")
    if raw is None or not raw.strip():
        return LEGACY_EXTRACTION
    value = raw.strip().lower()
    if value not in VALID_EXTRACTION_PROFILES:
        raise ValueError(
            f"CMF_EXTRACTION_PROFILE={raw!r} is not a recognised extraction profile. "
            f"Valid values are {', '.join(VALID_EXTRACTION_PROFILES)}."
        )
    return value


def _embedding_dim() -> int:
    raw = os.getenv("EMBEDDING_DIM")
    if raw is None or not raw.strip():
        return DEFAULT_EMBEDDING_DIM
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"EMBEDDING_DIM={raw!r} is not an integer.") from None
    if value <= 0:
        raise ValueError(f"EMBEDDING_DIM={raw!r} must be positive.")
    return value


def load_config() -> CMFConfig:
    """Read the current environment into a CMFConfig snapshot.

    Calls load_dotenv() itself rather than depending on some other module
    having already loaded .env as an import-time side effect (see
    server.providers.wiki.provider.FileKnowledgeProvider.is_configured
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
        llm_provider=_provider("CMF_LLM_PROVIDER"),
        embed_provider=_provider("CMF_EMBED_PROVIDER"),
        local_base_url=os.getenv("CMF_LOCAL_BASE_URL", DEFAULT_LOCAL_BASE_URL),
        local_api_key=os.getenv("CMF_LOCAL_API_KEY", DEFAULT_LOCAL_API_KEY),
        local_llm_model=os.getenv("CMF_LOCAL_LLM_MODEL", DEFAULT_LOCAL_LLM_MODEL),
        local_embed_model=os.getenv("CMF_LOCAL_EMBED_MODEL", DEFAULT_LOCAL_EMBED_MODEL),
        local_structured_mode=os.getenv("CMF_LOCAL_STRUCTURED_MODE", DEFAULT_LOCAL_STRUCTURED_MODE),
        embedding_dim=_embedding_dim(),
        extraction_profile=extraction_profile_from_env(),
    )
