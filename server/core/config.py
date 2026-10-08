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
import getpass
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

GEMINI_PROVIDER = "gemini"
LOCAL_PROVIDER = "local"
ANTHROPIC_PROVIDER = "anthropic"
OPENAI_PROVIDER = "openai"
VALID_PROVIDERS = (GEMINI_PROVIDER, LOCAL_PROVIDER, ANTHROPIC_PROVIDER, OPENAI_PROVIDER)
# Anthropic has no embeddings API, so it can only be an LLM provider.
VALID_EMBED_PROVIDERS = (GEMINI_PROVIDER, LOCAL_PROVIDER, OPENAI_PROVIDER)

# Hosted-API model defaults (MS10a), overridable per provider. The Claude
# default is the current Sonnet (User, 2026-10-08): capture runs dozens of
# extraction calls per long transcript. claude-opus-5-5 is the step up,
# claude-haiku-5-5 the cheaper step down. gpt-5.5 is Graphiti 0.29.3's own
# OpenAI default.
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
DEFAULT_OPENAI_MODEL = "gpt-5.5"
DEFAULT_OPENAI_EMBED_MODEL = "text-embedding-3-small"
VALID_ANTHROPIC_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# MS4e: which entity-extraction profile add_episode() runs under (see
# server.providers.extraction_profile). "typed-recall" is the default since
# MS10a (2026-10-08); "legacy", the pre-MS4e behaviour, is opt-in.
LEGACY_EXTRACTION = "legacy"
TYPED_RECALL_EXTRACTION = "typed-recall"
VALID_EXTRACTION_PROFILES = (LEGACY_EXTRACTION, TYPED_RECALL_EXTRACTION)

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
    extraction_profile: str = TYPED_RECALL_EXTRACTION

    # Per-role local endpoints (MS10a): an LM Studio LLM and an Ollama
    # embedder can live on different hosts/ports, and a hosted LLM can pair
    # with a laptop embedder. Each falls back to local_base_url/local_api_key
    # in load_config(), so a single CMF_LOCAL_BASE_URL keeps working.
    local_llm_base_url: str = DEFAULT_LOCAL_BASE_URL
    local_llm_api_key: str = DEFAULT_LOCAL_API_KEY
    local_embed_base_url: str = DEFAULT_LOCAL_BASE_URL
    local_embed_api_key: str = DEFAULT_LOCAL_API_KEY

    # Hosted APIs (MS10a), reached with the operator's own keys.
    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL
    openai_model: str = DEFAULT_OPENAI_MODEL
    openai_embed_model: str = DEFAULT_OPENAI_EMBED_MODEL
    # None = the model's own default effort (high on claude-sonnet-5-5).
    anthropic_effort: Optional[str] = None

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
        return self.llm_credential_ok and self.embed_credential_ok

    @property
    def llm_credential_ok(self) -> bool:
        """The selected LLM provider has its key or endpoint."""
        return self._credential_ok(self.llm_provider, self.local_llm_base_url)

    @property
    def embed_credential_ok(self) -> bool:
        """The selected embedding provider has its key or endpoint."""
        return self._credential_ok(self.embed_provider, self.local_embed_base_url)

    def _credential_ok(self, provider: str, local_url: str) -> bool:
        if provider == GEMINI_PROVIDER:
            return bool(self.gemini_api_key)
        if provider == LOCAL_PROVIDER:
            return bool(local_url and local_url.strip())
        if provider == ANTHROPIC_PROVIDER:
            return bool(self.anthropic_api_key)
        if provider == OPENAI_PROVIDER:
            return bool(self.openai_api_key)
        return False


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


def default_reviewer() -> str:
    """Who a review verdict is recorded against when the caller names no one:
    CMF_REVIEWER, else the OS login name."""
    raw = os.getenv("CMF_REVIEWER")
    if raw and raw.strip():
        return raw.strip()
    try:
        return getpass.getuser()
    except Exception:  # no login name in some containers
        return "user"


def _embed_provider() -> str:
    """CMF_EMBED_PROVIDER, which also rejects a provider with no embeddings API."""
    value = _provider("CMF_EMBED_PROVIDER")
    if value not in VALID_EMBED_PROVIDERS:
        raise ValueError(
            f"CMF_EMBED_PROVIDER={value!r}: {value} has no embeddings API. "
            f"Use one of {', '.join(VALID_EMBED_PROVIDERS)} (for example openai, or a local "
            "embedder such as Ollama via CMF_LOCAL_EMBED_BASE_URL; see docs/SETUP.md)."
        )
    return value


def _anthropic_effort() -> Optional[str]:
    raw = os.getenv("CMF_ANTHROPIC_EFFORT")
    if raw is None or not raw.strip():
        return None
    value = raw.strip().lower()
    if value not in VALID_ANTHROPIC_EFFORTS:
        raise ValueError(
            f"CMF_ANTHROPIC_EFFORT={raw!r} is not a recognised effort. "
            f"Valid values are {', '.join(VALID_ANTHROPIC_EFFORTS)}."
        )
    return value


def _env_or(name: str, fallback: str) -> str:
    """`name` when set and non-blank, else `fallback`."""
    value = os.getenv(name)
    return value if value and value.strip() else fallback


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

    Unset means "typed-recall", the production profile since 2026-09-28;
    "legacy" is opt-in, for reproducing older extraction. Same fail-loudly
    reasoning as _provider(): a typo silently falling back to the default
    would make an A/B replay measure the wrong thing, and the only symptom
    would be a graph that looks exactly like the baseline.
    """
    raw = os.getenv("CMF_EXTRACTION_PROFILE")
    if raw is None or not raw.strip():
        return TYPED_RECALL_EXTRACTION
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
    local_base_url = os.getenv("CMF_LOCAL_BASE_URL", DEFAULT_LOCAL_BASE_URL)
    local_api_key = os.getenv("CMF_LOCAL_API_KEY", DEFAULT_LOCAL_API_KEY)
    return CMFConfig(
        llm_wiki_path=os.getenv("LLM_WIKI_PATH"),
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        falkordb_host=os.getenv("FALKORDB_HOST", "localhost"),
        falkordb_port=int(os.getenv("FALKORDB_PORT", "6379")),
        falkordb_password=os.getenv("FALKORDB_PASSWORD") or None,
        falkordb_database=os.getenv("FALKORDB_DATABASE"),
        cmf_state_dir=_resolve_state_dir(),
        llm_provider=_provider("CMF_LLM_PROVIDER"),
        embed_provider=_embed_provider(),
        local_base_url=local_base_url,
        local_api_key=local_api_key,
        local_llm_model=_env_or("CMF_LOCAL_LLM_MODEL", DEFAULT_LOCAL_LLM_MODEL),
        local_embed_model=_env_or("CMF_LOCAL_EMBED_MODEL", DEFAULT_LOCAL_EMBED_MODEL),
        local_structured_mode=_env_or("CMF_LOCAL_STRUCTURED_MODE", DEFAULT_LOCAL_STRUCTURED_MODE),
        embedding_dim=_embedding_dim(),
        extraction_profile=extraction_profile_from_env(),
        local_llm_base_url=_env_or("CMF_LOCAL_LLM_BASE_URL", local_base_url),
        local_llm_api_key=_env_or("CMF_LOCAL_LLM_API_KEY", local_api_key),
        local_embed_base_url=_env_or("CMF_LOCAL_EMBED_BASE_URL", local_base_url),
        local_embed_api_key=_env_or("CMF_LOCAL_EMBED_API_KEY", local_api_key),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        anthropic_model=_env_or("CMF_ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL),
        openai_model=_env_or("CMF_OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
        openai_embed_model=_env_or("CMF_OPENAI_EMBED_MODEL", DEFAULT_OPENAI_EMBED_MODEL),
        anthropic_effort=_anthropic_effort(),
    )
