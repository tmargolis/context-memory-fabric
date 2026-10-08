"""Episodic memory integration with Graphiti and FalkorDB.

Responsible for saving episodic events, decisions, preferences, and state changes,
recalling relevant graph knowledge, and formatting temporal memories with provenance.

Relocated here from server/memory.py in Milestone 1 (see
docs/adr/0002-provider-boundaries.md); server.memory is now a thin
backward-compatibility re-export shim so existing imports and
unittest.mock.patch("server.memory.X") call sites keep working unchanged.
GraphitiMemoryProvider at the bottom of this file is a thin class wrapper
around the module-level functions below, satisfying
server.core.protocols.MemoryProvider — the functions themselves are
unchanged from their pre-Milestone-1 form.
"""

import asyncio
import contextlib
import contextvars
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Literal, Optional, Union, overload

from dotenv import load_dotenv
from graphiti_core import Graphiti
from graphiti_core.cross_encoder.gemini_reranker_client import GeminiRerankerClient
from graphiti_core.embedder.gemini import GeminiEmbedder, GeminiEmbedderConfig
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.gemini_client import GeminiClient
from graphiti_core.nodes import EpisodeType

from server.core.config import ANTHROPIC_PROVIDER, GEMINI_PROVIDER, OPENAI_PROVIDER, CMFConfig, load_config
from server.core.rate_limiter import (
    GeminiQuotaExhaustedError,
    classify_transient_error as _classify_transient_error,
    get_default_rate_limiter,
    is_transient_gemini_error as _is_transient_gemini_error,
)
from server.providers.episode_postprocess import postprocess_episode
from server.providers.extraction_profile import LEGACY_INSTRUCTIONS, extraction_kwargs
from server.providers.falkor_driver import CMFFalkorDriver, falkordb_connection_params

load_dotenv()
logger = logging.getLogger(__name__)

# Cached Graphiti instances per (event_loop_id, graph_name, model)
_GRAPHITI_INSTANCES: dict[tuple[Optional[int], str], Graphiti] = {}

# graphiti_core's own FalkorDB driver logs `Index already exists: ...` at INFO
# every time add_episode() re-attempts index creation -- which is every call,
# since the indices are already there after the first run. Never informative
# past that first run; pure per-episode noise (2026-09-15 log-verbosity pass).
logging.getLogger("graphiti_core.driver.falkordb_driver").setLevel(logging.WARNING)

# httpx logs one bare `HTTP Request: POST .../embeddings "HTTP/1.1 200 OK"` (or
# .../chat/completions) line per call, with no indication of which higher-level
# operation triggered it or why -- a single remember() episode can produce
# 15-20 of these. Rather than lose that signal entirely, count them (via
# contextvars so concurrent remember_queued() background tasks don't
# cross-contaminate counts -- asyncio.create_task() copies the current
# context, so each background task's counters start independently at 0) and
# suppress the raw per-request line; remember()/recall_mem() log one summary
# instead, attributing the counts to the operation that caused them.
_embedding_call_count: contextvars.ContextVar[int] = contextvars.ContextVar(
    "_embedding_call_count", default=0
)
_completion_call_count: contextvars.ContextVar[int] = contextvars.ContextVar(
    "_completion_call_count", default=0
)


class _HttpxCallCounterFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.INFO:
            return True  # never suppress a genuine httpx warning/error
        msg = record.getMessage()
        if "/embeddings" in msg:
            _embedding_call_count.set(_embedding_call_count.get() + 1)
            return False
        if "/chat/completions" in msg:
            _completion_call_count.set(_completion_call_count.get() + 1)
            return False
        return True


# Both loggers are attached: this venv has both `httpx` (0.28.1) and `httpx2`
# (2.12.0) installed, and the openai SDK version in use here is built on
# httpx2 specifically (`logging.getLogger("httpx2")` in httpx2/_client.py) --
# confirmed 2026-09-15 after a first attempt at this fix silently counted
# zero calls because it only covered the "httpx" name.
for _httpx_logger_name in ("httpx", "httpx2"):
    logging.getLogger(_httpx_logger_name).addFilter(_HttpxCallCounterFilter())


def _call_counts() -> tuple[int, int]:
    return _embedding_call_count.get(), _completion_call_count.get()


def _log_query(query: str) -> str:
    """First 3 words of a query, for log lines — the real query (often a long
    LLM-synthesized topic string) is noise past that point; full text isn't
    lost, it's just not in the log (the user, 2026-09-16)."""
    words = query.split()
    return " ".join(words[:3]) + ("..." if len(words) > 3 else "")

# Extraction kwargs for add_episode() live in server.providers.extraction_profile
# (MS4e), selected by CMF_EXTRACTION_PROFILE. EXTRACTION_INSTRUCTIONS is kept
# as a name for the legacy text, which scripts/ms6b_exit_gate.py still imports.
EXTRACTION_INSTRUCTIONS = LEGACY_INSTRUCTIONS

# A lone `_` breaks FalkorDB's RediSearch fulltext queries. Graphiti builds
# them by turning every other ASCII punctuation character into a space
# (graphiti_core.driver.falkordb.fulltext._SEPARATOR_MAP, which leaves `_`
# out) and joining the surviving words with ` | `, so text like
# `group_id: "_"` becomes `(group_id | _ | ...)` and raises "Syntax error ...
# near group_id" -- a real, deterministic promotion failure (2026-09-21).
# Probed against live FalkorDB: a bare `_` is the only token that breaks;
# `__`, `a_`, `_a`, `group_id` and empty `""` all pass. Graphiti builds these
# queries from episode text, the facts extracted from it, and search
# queries, through two separate code paths (the driver method and
# driver/falkordb/operations/search_ops.py), so the fix goes on CMF's side,
# on the text before it reaches Graphiti, rather than patching graphiti_core.
# The replacement is U+FF3F FULLWIDTH LOW LINE: it reads the same, isn't in
# the separator map, and passes as a query token.
_FULLTEXT_SEPARATORS = re.escape(",.<>{}[]\"':;!@#$%^&*()-+=~?|/\\`")
_LONE_UNDERSCORE = re.compile(rf"(?<![^\s{_FULLTEXT_SEPARATORS}])_(?![^\s{_FULLTEXT_SEPARATORS}])")


def neutralize_fulltext_hazards(text: str) -> str:
    """Replace each `_` that Graphiti's fulltext tokenizer would leave as a bare
    token with a fullwidth low line. Underscores inside words are untouched."""
    return _LONE_UNDERSCORE.sub("\uff3f", text)


# _classify_transient_error / _is_transient_gemini_error, used by
# remember()/recall()'s retry loops below, are imported (and re-exported
# under these same names, for every existing call site) from
# server.core.rate_limiter — see that module for the full rationale.


class MissingGraphConfigurationError(RuntimeError):
    """Raised when no FalkorDB target graph can be resolved.

    FALKORDB_DATABASE previously defaulted silently to "default_db" when unset,
    which caused production reads/writes to diverge from the graph an operator
    believed was configured (see docs/adr/0003-graph-and-state-topology.md).
    As of MS6c, `resolve_target_database()` falls back to `DEFAULT_GRAPH_NAME`
    instead of raising — kept importable for callers that still want to treat
    an unresolved graph as fatal.
    """


# MS6c: deliberately a non-production sandbox, not mem-fabric-local-ep or
# mem-fabric-local-wiki, so a client with no FALKORDB_DATABASE configured
# can't silently read or write production data during harness comparison
# testing. Still overridden by an explicit graph_name or FALKORDB_DATABASE.
DEFAULT_GRAPH_NAME = "default_db"


def resolve_target_database(graph_name: Optional[str] = None) -> str:
    """Resolve the FalkorDB graph name to use.

    Args:
        graph_name: Explicit override (e.g. from an MCP tool argument). Takes
            precedence over environment configuration when non-empty.

    Returns:
        The resolved graph name: `graph_name` if given, else
        `FALKORDB_DATABASE` if set, else `DEFAULT_GRAPH_NAME`.
    """
    if graph_name and graph_name.strip():
        return graph_name.strip()

    env_value = os.getenv("FALKORDB_DATABASE")
    if env_value and env_value.strip():
        return env_value.strip()

    return DEFAULT_GRAPH_NAME


def _require_gemini_key() -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set. Add it to the project-root .env file.")
    return api_key


def _require_key(value: Optional[str], env_var: str, provider: str) -> str:
    if not value:
        raise RuntimeError(f"{env_var} is not set, but {provider} is a configured provider. Add it to .env.")
    return value


def _build_llm_client(config: CMFConfig, resolved_model: str):
    """LLM client for entity/fact extraction, per CMF_LLM_PROVIDER."""
    if config.llm_provider == ANTHROPIC_PROVIDER:
        from server.providers.anthropic_client import StructuredAnthropicClient

        api_key = _require_key(config.anthropic_api_key, "ANTHROPIC_API_KEY", "anthropic")
        return StructuredAnthropicClient(
            config=LLMConfig(api_key=api_key, model=resolved_model, small_model=resolved_model),
            effort=config.anthropic_effort,
        )

    if config.llm_provider == OPENAI_PROVIDER:
        from graphiti_core.llm_client.openai_client import OpenAIClient

        api_key = _require_key(config.openai_api_key, "OPENAI_API_KEY", "openai")
        return OpenAIClient(config=LLMConfig(api_key=api_key, model=resolved_model, small_model=resolved_model))

    if not config.llm_is_local:
        api_key = _require_gemini_key()
        return GeminiClient(
            config=LLMConfig(api_key=api_key, model=resolved_model, small_model=resolved_model)
        )

    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

    from server.providers.lmstudio_client import LMStudioCompatClient

    # OpenAIGenericClient, not OpenAIClient: the generic one targets arbitrary
    # OpenAI-compatible endpoints. The injected client applies LM Studio's two
    # required fixups — see server/providers/lmstudio_client.py.
    return OpenAIGenericClient(
        config=LLMConfig(
            api_key=config.local_llm_api_key,
            base_url=config.local_llm_base_url,
            model=resolved_model,
            small_model=resolved_model,
        ),
        client=LMStudioCompatClient(config.local_llm_base_url, config.local_llm_api_key),
        structured_output_mode=config.local_structured_mode,
    )


def _build_embedder(config: CMFConfig):
    """Embedder, per CMF_EMBED_PROVIDER.

    The explicit `embedding_dim` is load-bearing in every branch:
    OpenAIEmbedder slices every vector to `embedding[: embedding_dim]`, so a
    value wider than the model actually returns is a silent no-op while a
    narrower one silently truncates. Neither raises. `assert_embedding_width`
    below is the guard that turns that into a real failure.
    """
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig

    if config.embed_provider == OPENAI_PROVIDER:
        from server.providers.openai_embedder import DimensionedOpenAIEmbedder

        api_key = _require_key(config.openai_api_key, "OPENAI_API_KEY", "openai")
        return DimensionedOpenAIEmbedder(
            config=OpenAIEmbedderConfig(
                api_key=api_key,
                embedding_model=config.openai_embed_model,
                embedding_dim=config.embedding_dim,
            )
        )

    if not config.embed_is_local:
        from server.providers.metered_embedder import maybe_meter

        api_key = _require_gemini_key()
        # Wrapped so embedding calls are debited against the free tier. They
        # were not, and at ~20 embeddings per episode they are the ceiling CMF
        # reaches first — see server/providers/metered_embedder.py.
        return maybe_meter(
            GeminiEmbedder(
                config=GeminiEmbedderConfig(
                    api_key=api_key,
                    embedding_model="gemini-embedding-001",
                )
            ),
            embed_is_local=False,
        )

    return OpenAIEmbedder(
        config=OpenAIEmbedderConfig(
            api_key=config.local_embed_api_key,
            base_url=config.local_embed_base_url,
            embedding_model=config.local_embed_model,
            embedding_dim=config.embedding_dim,
        )
    )


def _build_cross_encoder(config: CMFConfig, resolved_model: str):
    """Reranker, per CMF_LLM_PROVIDER.

    Local and Anthropic deployments use CMF's own reranker choice
    (CMF_RERANKER, default passthrough): Graphiti's LLM rerankers score from
    token logprobs, which Claude's API doesn't return, and
    `OpenAIRerankerClient` is specifically unusable against LM Studio: it
    scores via `logit_bias` on hardcoded OpenAI BPE token ids, which map to
    unrelated tokens under GLM's or Qwen's tokenizer. See
    server/providers/reranker.py. On OpenAI it is the real thing, on
    Graphiti's own small default model (logprobs need a non-reasoning model).
    """
    if config.llm_provider == OPENAI_PROVIDER:
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient

        api_key = _require_key(config.openai_api_key, "OPENAI_API_KEY", "openai")
        return OpenAIRerankerClient(config=LLMConfig(api_key=api_key))

    if config.llm_provider == GEMINI_PROVIDER:
        api_key = _require_gemini_key()
        return GeminiRerankerClient(config=LLMConfig(api_key=api_key, model=resolved_model))

    from server.providers.reranker import PASSTHROUGH, make_cross_encoder

    return make_cross_encoder(os.getenv("CMF_RERANKER", PASSTHROUGH))


def resolve_llm_model(config: Optional[CMFConfig] = None, model: Optional[str] = None) -> str:
    """The model id for extraction, from whichever provider is configured.

    The rate limiter's chain is a list of *Gemini* model ids, so it is only a
    meaningful default when extraction actually runs on Gemini. On the other
    paths the model comes from that provider's own setting.
    """
    if model:
        return model
    config = config or load_config()
    if config.llm_is_local:
        return config.local_llm_model
    if config.llm_provider == ANTHROPIC_PROVIDER:
        return config.anthropic_model
    if config.llm_provider == OPENAI_PROVIDER:
        return config.openai_model
    return get_default_rate_limiter().chain[0]


def create_graphiti(graph_name: Optional[str] = None, model: Optional[str] = None) -> Graphiti:
    """Instantiate a Graphiti client configured with FalkorDB and the configured providers.

    `model` selects the LLM used for entity/fact extraction and reranking.
    Defaults to the first model in the rate limiter's chain on the Gemini
    path, or CMF_LOCAL_LLM_MODEL on the local one — used for construction
    paths (edit_memory, reconcile_memories' Cypher access) that need a
    Graphiti instance but never actually invoke the LLM client, so no
    rate-limit reservation applies there. Callers that DO invoke the LLM
    (remember/recall) must go through get_graphiti_for_operation() instead,
    which reserves a model from the rate limiter first.

    Extraction and embedding are selected independently (CMF_LLM_PROVIDER /
    CMF_EMBED_PROVIDER) so the hybrid — local embeddings, Gemini extraction —
    is a config change. That is not hypothetical: the Gemini embedding quota
    (1,000/day) is unmetered by the rate limiter and is the ceiling CMF hits
    first. See docs/SPARK-MIGRATION-PLAN.md.
    """
    config = load_config()

    target_database = resolve_target_database(graph_name)
    resolved_model = resolve_llm_model(config, model)

    # CMFFalkorDriver replaces Graphiti's slow edge full-text join (see
    # server/providers/falkor_driver.py); it is otherwise a plain FalkorDriver.
    driver = CMFFalkorDriver(**falkordb_connection_params(), database=target_database)

    return Graphiti(
        graph_driver=driver,
        llm_client=_build_llm_client(config, resolved_model),
        embedder=_build_embedder(config),
        cross_encoder=_build_cross_encoder(config, resolved_model),
    )


async def assert_embedding_width(graphiti: Graphiti, expected: Optional[int] = None) -> int:
    """Embed a probe string and fail loudly if the width is not as configured.

    Exists because every layer here fails silently on a width mismatch:
    OpenAIEmbedder slices rather than raises, FalkorDB accepts whatever it is
    handed, and graphiti_core.search falls back to `[0.0] * EMBEDDING_DIM`
    from a module constant frozen at import. A graph can therefore be built
    entirely from vectors of the wrong width with nothing raised anywhere —
    and the symptom is merely poor recall, which is easy to misread as a
    model-quality problem. Call this once after switching providers.
    """
    expected = expected if expected is not None else load_config().embedding_dim
    vector = await graphiti.embedder.create(input_data="cmf embedding width probe")
    actual = len(vector)
    if actual != expected:
        raise RuntimeError(
            f"Embedder returned {actual}-dimension vectors but EMBEDDING_DIM is {expected}. "
            f"Writing these into a {expected}-dimension graph would corrupt it silently. "
            f"Set EMBEDDING_DIM={actual} and point FALKORDB_DATABASE at a fresh graph."
        )
    return actual


def get_graphiti(graph_name: Optional[str] = None, model: Optional[str] = None) -> Graphiti:
    """Retrieve or initialize the Graphiti instance for the active event loop, target graph database, and model.

    `model` defaults to the rate limiter's first chain preference when
    omitted (see create_graphiti's docstring) — this default does NOT
    reserve rate-limit headroom, since most callers of get_graphiti() never
    invoke the LLM client at all. Use get_graphiti_for_operation() for calls
    that do.
    """
    global _GRAPHITI_INSTANCES

    try:
        current_loop = asyncio.get_running_loop()
        current_loop_id = id(current_loop)
    except RuntimeError:
        current_loop_id = None

    target_database = resolve_target_database(graph_name)
    resolved_model = model or get_default_rate_limiter().chain[0]
    cache_key = (current_loop_id, target_database, resolved_model)

    if cache_key not in _GRAPHITI_INSTANCES:
        _GRAPHITI_INSTANCES[cache_key] = create_graphiti(graph_name=target_database, model=resolved_model)

    return _GRAPHITI_INSTANCES[cache_key]


def get_graphiti_for_operation(graph_name: Optional[str] = None) -> tuple[Graphiti, str]:
    """Reserve a model from the free-tier rate limiter, then return the matching Graphiti instance.

    This is the entry point for any code path that actually calls into
    Gemini (remember() via add_episode, recall_mem() via search()) — it commits
    a reservation against the local ledger before construction/reuse of the
    Graphiti client, so a caller that gets GeminiQuotaExhaustedError from
    this function has made zero Gemini calls and can safely defer the
    operation rather than having partially spent quota on a call that then
    also failed.
    """
    chosen_model = get_default_rate_limiter().reserve()
    return get_graphiti(graph_name=graph_name, model=chosen_model), chosen_model


async def close_graphiti() -> None:
    """Explicitly close all active Graphiti clients and their associated resources."""
    global _GRAPHITI_INSTANCES
    instances = list(_GRAPHITI_INSTANCES.values())
    _GRAPHITI_INSTANCES.clear()

    for instance in instances:
        try:
            # Close LLM client if open
            if hasattr(instance, "llm_client") and hasattr(instance.llm_client, "client"):
                c = getattr(instance.llm_client, "client")
                if hasattr(c, "aclose"):
                    try:
                        await c.aclose()
                    except Exception:
                        pass
            # Close embedder client if open
            if hasattr(instance, "embedder") and hasattr(instance.embedder, "client"):
                c = getattr(instance.embedder, "client")
                if hasattr(c, "aclose"):
                    try:
                        await c.aclose()
                    except Exception:
                        pass
            await instance.close()
        except Exception as e:
            logger.debug(f"Error closing Graphiti driver: {e}")

    import gc
    gc.collect()


def _slug(value: Optional[str]) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return s or "misc"


async def _next_semantic_seq(driver: Any, prefix: str, target_graph: Optional[str] = None) -> int:
    """Find the next sequence number (NNN) for a prefix across FalkorDB and the promotions ledger."""
    best = 0
    safe_prefix = re.sub(r"[^a-z0-9_-]+", "", prefix.lower())
    try:
        res = await driver.execute_query(
            f"MATCH (e:Episodic) WHERE e.name STARTS WITH '{safe_prefix}-' RETURN e.name"
        )
        rows = res[0] if res and isinstance(res[0], list) else (res or [])
        for r in rows:
            ep_name = (r.get("e.name") or "").strip()
            tail = ep_name.rsplit("-", 1)[-1]
            if tail.isdigit():
                best = max(best, int(tail))
    except Exception as e:
        logger.debug(f"Error querying max sequence in graph for {prefix}: {e}")

    try:
        from server.consolidation.promotion import PromotionStore

        with PromotionStore() as ps:
            graph_scope = target_graph or resolve_target_database()
            best = max(best, ps.max_semantic_seq(safe_prefix, graph_scope))
    except Exception as e:
        logger.debug(f"Error querying max sequence in ledger for {prefix}: {e}")

    return best + 1


async def resolve_remember_identity(
    driver: Any,
    content: str,
    name: Optional[str] = None,
    project: Optional[str] = None,
    harness: Optional[str] = None,
    source_description: Optional[str] = None,
    target_graph: Optional[str] = None,
) -> tuple[str, str, Optional[str]]:
    """Resolve (episode_name, project_slug, harness) conforming to <harness>-<project>-NNN."""
    resolved_harness = harness
    sd = source_description or ""

    if not resolved_harness or resolved_harness in ("unknown", "unknown_mcp_client"):
        sd_lower = sd.lower()
        if "chatgpt" in sd_lower or "openai" in sd_lower:
            resolved_harness = "chatgpt"
        elif "claude_desktop_code" in sd_lower or "claude desktop code" in sd_lower:
            resolved_harness = "claude_desktop_code"
        elif "claude_cowork" in sd_lower or "cowork" in sd_lower:
            resolved_harness = "claude_cowork"
        elif "claude code" in sd_lower or "claude_code" in sd_lower:
            resolved_harness = "claude_code"
        elif "claude desktop" in sd_lower or "claude_desktop" in sd_lower:
            resolved_harness = "claude_desktop"
        elif "claude" in sd_lower:
            resolved_harness = "claude"
        elif "gemini" in sd_lower:
            resolved_harness = "gemini"
        elif "antigravity" in sd_lower:
            resolved_harness = "antigravity"
        elif name:
            # Longest prefixes first: "claude-desktop-code-x" also starts with "claude-desktop-".
            for h in ("claude-desktop-code", "claude-cowork", "claude-code", "claude-desktop", "claude", "chatgpt", "gemini", "antigravity"):
                if name.lower().startswith(f"{h}-"):
                    resolved_harness = h.replace("-", "_")
                    break

    resolved_project = None
    if project and project.strip():
        resolved_project = _slug(project)
    elif name and re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*-\d{3,}$", name.lower()):
        # name is already formatted as <harness>-<project>-NNN
        if resolved_harness:
            h_slug = _slug(resolved_harness)
            prefix_to_strip = f"{h_slug}-"
            if name.lower().startswith(prefix_to_strip):
                resolved_project = name.lower()[len(prefix_to_strip):].rsplit("-", 1)[0]
    if not resolved_project and sd:
        m_proj = re.search(r"project=([^\s|]+)", sd)
        if m_proj:
            resolved_project = _slug(m_proj.group(1))
    if not resolved_project:
        try:
            from server.review.projects import classify

            classified = classify(thread_key=name, statement=content)
            resolved_project = _slug(classified)
        except Exception:
            resolved_project = "misc"

    h_slug = _slug(resolved_harness) if resolved_harness and resolved_harness != "unknown" else None
    if name and re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*-\d{3,}$", name.lower()):
        episode_name = name.lower()
    elif h_slug:
        prefix = f"{h_slug}-{resolved_project}"
        next_seq = await _next_semantic_seq(driver, prefix, target_graph)
        episode_name = f"{prefix}-{next_seq:03d}"
    else:
        if name:
            episode_name = name
        else:
            prefix = f"mcp-{resolved_project}"
            next_seq = await _next_semantic_seq(driver, prefix, target_graph)
            episode_name = f"{prefix}-{next_seq:03d}"

    return episode_name, resolved_project, resolved_harness


async def remember(
    content: str,
    name: Optional[str] = None,
    source_description: str = "Context Memory Fabric MCP",
    reference_time: Optional[datetime] = None,
    max_retries: int = 3,
    project: Optional[str] = None,
    harness: Optional[str] = None,
) -> dict[str, Any]:
    """Ingest a new episode into episodic memory (Graphiti + FalkorDB).

    Args:
        content: The text content to store as an episodic memory.
        name: An identifier name for the episode (auto-generated as <harness>-<project>-NNN if omitted).
        source_description: Description of the memory origin.
        reference_time: Time when the event occurred (defaults to now UTC).
        max_retries: Number of retries on transient rate limits (429).
        project: Optional project slug (e.g. 'ev-charging', 'photo').
        harness: Optional originating harness name (e.g. 'chatgpt', 'claude_code').

    Raises:
        GeminiQuotaExhaustedError: if every model in the configured free-tier
            chain lacks headroom right now. No Gemini call is made in this
            case — safe to retry later (e.g. next RPM window or day
            boundary) without having burned any quota on a failed attempt.
    """
    graphiti, chosen_model = get_graphiti_for_operation()
    ref_time = reference_time or datetime.now(timezone.utc)
    target_db = resolve_target_database()

    episode_name, resolved_project, resolved_harness = await resolve_remember_identity(
        driver=graphiti.driver,
        content=content,
        name=name,
        project=project,
        harness=harness,
        source_description=source_description,
        target_graph=target_db,
    )

    logger.info(
        f"Ingesting memory episode: '{episode_name}' (project: {resolved_project}, harness: {resolved_harness}, model: {chosen_model})"
    )
    call_start = time.monotonic()
    embed_before, comp_before = _call_counts()

    result = None
    for attempt in range(max_retries):
        try:
            result = await graphiti.add_episode(
                name=episode_name,
                episode_body=neutralize_fulltext_hazards(content),
                source_description=source_description,
                reference_time=ref_time,
                source=EpisodeType.text,
                **extraction_kwargs(source_description=source_description),
            )
            break
        except Exception as e:
            if _is_transient_gemini_error(e) and attempt < max_retries - 1:
                backoff = 25.0 * (attempt + 1)
                logger.warning(
                    f"Transient provider error in remember() ({_classify_transient_error(e)}). "
                    f"Retrying in {backoff:.1f}s (attempt {attempt+1}/{max_retries})..."
                )
                await asyncio.sleep(backoff)
            else:
                raise

    post = await postprocess_episode(
        graphiti, result, episode_name=episode_name, content=content, source_description=source_description,
    )

    # Apply inline tagging (harness label, project label + e.project property, and Project node link)
    try:
        from server.consolidation.graph_tagging import tag_promoted_episode

        await tag_promoted_episode(graphiti.driver, episode_name, resolved_project, resolved_harness)
    except Exception as e:
        logger.warning(f"tag_promoted_episode failed for {episode_name}: {e}")

    embed_after, comp_after = _call_counts()
    logger.info(
        f"Ingested '{episode_name}': {embed_after - embed_before} embedding + "
        f"{comp_after - comp_before} completion call(s) in {time.monotonic() - call_start:.1f}s"
    )

    return {
        "status": "success",
        "name": episode_name,
        "project": resolved_project,
        "harness": resolved_harness,
        "reference_time": ref_time.isoformat(),
        "source_description": source_description,
        "message": f"Successfully remembered episode '{episode_name}' in episodic memory.",
        "debris_removed": post["debris_removed"],
        "project_entity": post["project_entity"],
        "episode_embedded": post["embedded"],
    }


# Strong references for remember_queued()'s background tasks. asyncio only
# holds a *weak* reference to a task once nothing else does, so a bare
# `asyncio.create_task(...)` with no reference kept can be garbage-collected
# mid-flight -- the documented footgun the stdlib itself warns about. Each
# task removes itself on completion via add_done_callback.
_BACKGROUND_REMEMBER_TASKS: set[asyncio.Task] = set()


async def remember_queued(
    content: str,
    name: Optional[str] = None,
    source_description: str = "Context Memory Fabric MCP",
    reference_time: Optional[datetime] = None,
    max_retries: int = 3,
    project: Optional[str] = None,
    harness: Optional[str] = None,
) -> dict[str, Any]:
    """Queue an episode for background ingestion via remember() and return immediately.

    Exists because remember()'s full round trip -- LLM entity/edge extraction
    plus embeddings, ~15-20 sequential calls on the local Spark model -- can
    take well over a minute, longer than the timeout of whatever fronts a
    remote MCP client (e.g. OpenAI's Secure MCP Tunnel for ChatGPT -- see
    docs/CLIENTS.md). That gateway was cutting the connection before
    add_episode()'s final FalkorDB write ever ran, silently dropping the
    memory with no exception anywhere and no journal record, since
    server.capture.middleware only journals a tool call after it returns
    (confirmed 2026-09-15: two ChatGPT `remember` calls fully processed
    through the LLM in the server log but landed in zero FalkorDB graphs).

    The MCP `remember` tool calls this instead of remember() directly so the
    client gets an immediate ack decoupled from the slow extraction. Callers
    that want a synchronous, fully-confirmed write (e.g.
    promote_auto_accepted_memories, a local admin job with no tunnel in its
    path) should keep calling remember() directly.

    Returns immediately with status "queued", not "success" -- the caller
    cannot assume the episode is durable yet. Use recall_mem afterward to
    confirm a specific episode has actually landed.
    """
    ref_time = reference_time or datetime.now(timezone.utc)
    target_db = resolve_target_database()
    graphiti = get_graphiti(graph_name=target_db)

    episode_name, resolved_project, resolved_harness = await resolve_remember_identity(
        driver=graphiti.driver,
        content=content,
        name=name,
        project=project,
        harness=harness,
        source_description=source_description,
        target_graph=target_db,
    )

    async def _run() -> None:
        try:
            await remember(
                content=content,
                name=episode_name,
                source_description=source_description,
                reference_time=ref_time,
                max_retries=max_retries,
                project=resolved_project,
                harness=resolved_harness,
            )
            logger.info(f"Background remember committed: '{episode_name}'")
        except Exception:
            logger.exception(f"Background remember FAILED to commit: '{episode_name}'")

    task = asyncio.create_task(_run())
    _BACKGROUND_REMEMBER_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_REMEMBER_TASKS.discard)

    return {
        "status": "queued",
        "name": episode_name,
        "project": resolved_project,
        "harness": resolved_harness,
        "reference_time": ref_time.isoformat(),
        "source_description": source_description,
        "message": (
            f"Episode '{episode_name}' queued for background ingestion -- not yet confirmed. "
            "On the local-model path this can take a minute or more; use recall_mem to confirm "
            "it has landed before assuming it failed or retrying."
        ),
    }


_PROVENANCE_FIELDS = (
    ("reasoning_kind", re.compile(r"reasoning_kind=(\S+)")),
    ("project", re.compile(r"project=(\S+)")),
    ("evidence", re.compile(r"evidence=(\d+)\s*turn")),
)


def _provenance_from_source_description(source_description: Optional[str]) -> str:
    """Condense a promoted episode's source_description into a one-line provenance tag.

    e.g. "reasoning_kind=decision \u00b7 project=openclaw \u00b7 evidence=2 turn(s)".
    Returns "" when none of the fields are present (non-promoted episode).
    """
    sd = source_description or ""
    parts = []
    for label, pat in _PROVENANCE_FIELDS:
        m = pat.search(sd)
        if not m:
            continue
        parts.append(f"evidence={m.group(1)} turn(s)" if label == "evidence" else f"{label}={m.group(1)}")
    return " \u00b7 ".join(parts)


_EPISODE_VECTOR_INDEX: dict[str, bool] = {}
_EPISODE_VECTOR_K = 6  # vector-arm candidates fed into the RRF merge (head only)


async def _graph_has_episode_vector_index(graphiti: Graphiti) -> bool:
    """True if the target graph carries a VECTOR index on Episodic.content_embedding.

    Cached per resolved graph name. When False, recall_mem's vector arm is a
    no-op, so the episode-content retrieval can ship before any graph is
    backfilled. See docs/spark-ms7-episode-vector-spike.md.
    """
    if os.getenv("CMF_MEM_EPISODE_VECTOR", "1") == "0":
        return False
    name = resolve_target_database()
    if name in _EPISODE_VECTOR_INDEX:
        return _EPISODE_VECTOR_INDEX[name]
    present = False
    try:
        rows = await graphiti.driver.execute_query(
            "CALL db.indexes() YIELD label, types RETURN label, types"
        )
        recs = rows[0] if rows and isinstance(rows[0], list) else (rows or [])
        for r in recs:
            if r.get("label") == "Episodic" and "content_embedding" in str(r.get("types")) and "VECTOR" in str(r.get("types")):
                present = True
                break
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"episode-vector index probe failed ({e!r}); treating as absent")
    _EPISODE_VECTOR_INDEX[name] = present
    return present


async def _episode_vector_search(graphiti: Graphiti, query: str, k: int) -> list[dict[str, Any]]:
    """KNN over Episodic.content_embedding — the synthesized statement itself,
    reachable even when qwen extracted zero entities for it. Returns fact-shaped
    dicts; [] if the graph has no such index or the query cannot be embedded."""
    if k <= 0 or not await _graph_has_episode_vector_index(graphiti):
        return []
    try:
        vec = await graphiti.embedder.create(query)
        lit = "[" + ",".join(f"{x:.7f}" for x in vec) + "]"
        rows = await graphiti.driver.execute_query(
            f"CALL db.idx.vector.queryNodes('Episodic', 'content_embedding', {int(k)}, vecf32({lit})) "
            "YIELD node, score "
            "RETURN node.uuid AS uuid, node.name AS name, node.content AS content, "
            "node.valid_at AS valid_at, score AS distance"
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"recall_mem: episode-vector search failed ({e!r}); edge results only")
        return []
    recs = rows[0] if rows and isinstance(rows[0], list) else (rows or [])
    out: list[dict[str, Any]] = []
    for r in recs:
        va = r.get("valid_at")
        out.append({
            "fact": r.get("content") or "",
            "valid_at": va.isoformat() if isinstance(va, datetime) else va,
            "invalid_at": None,
            "episodes": [r["uuid"]] if r.get("uuid") else [],
            "created_at": None,
            "_via": "episode_vector",
            "_distance": r.get("distance"),
        })
    return out


def _rrf_merge(
    *ranked_lists: list[dict[str, Any]], k: int = 60, limit: int, max_per_episode: int = 1
) -> list[dict[str, Any]]:
    """Reciprocal-rank fusion of fact lists, deduped by fact text (case-insensitive),
    then capped at `max_per_episode` facts per source episode.

    The per-episode cap stops one chatty episode (e.g. the three near-identical
    'crypto Summary method' RELATES_TO facts) from crowding out a distinct
    episode's fact further down the ranking. The first list a fact appears in
    supplies the kept dict."""
    score: dict[str, float] = {}
    keep: dict[str, dict[str, Any]] = {}
    for lst in ranked_lists:
        for rank, item in enumerate(lst):
            key = (item.get("fact") or "").strip().lower()
            if not key:
                continue
            score[key] = score.get(key, 0.0) + 1.0 / (k + rank)
            keep.setdefault(key, item)
    ordered = sorted(keep.values(), key=lambda it: -score[(it.get("fact") or "").strip().lower()])
    out: list[dict[str, Any]] = []
    per_ep: dict[frozenset, int] = {}
    for it in ordered:
        eps = frozenset(it.get("episodes") or [])
        if eps and per_ep.get(eps, 0) >= max_per_episode:
            continue
        out.append(it)
        if eps:
            per_ep[eps] = per_ep.get(eps, 0) + 1
        if len(out) >= limit:
            break
    return out


async def _resolve_episode_index(graphiti: Graphiti, uuids: list[str]) -> dict[str, dict[str, str]]:
    """uuid -> {name, content, provenance} for the given Episodic node uuids.

    One batched read. Best-effort: on any driver error, returns {} so callers
    degrade to bare facts rather than failing the whole recall.
    """
    uniq = sorted({u for u in uuids if u})
    if not uniq:
        return {}
    try:
        rows = await graphiti.driver.execute_query(
            "MATCH (e:Episodic) WHERE e.uuid IN $uuids "
            "RETURN e.uuid AS uuid, e.name AS name, e.content AS content, "
            "e.source_description AS source_description",
            uuids=uniq,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"recall_mem: source-episode resolution failed ({e!r}); returning bare facts")
        return {}
    recs = rows[0] if rows and isinstance(rows[0], list) else (rows or [])
    out: dict[str, dict[str, str]] = {}
    for r in recs:
        out[r["uuid"]] = {
            "name": r.get("name") or r["uuid"],
            "content": r.get("content") or "",
            "provenance": _provenance_from_source_description(r.get("source_description")),
        }
    return out


def format_memory_results_for_mcp(facts: list[dict[str, Any]], query: str) -> str:
    """Format extracted memory facts into clean Markdown for MCP tool responses."""
    if not facts:
        return f"No matching episodic memory found for query: '{query}'."

    lines = [
        f"### Episodic Memory Search Results for '{query}'",
        f"Retrieved {len(facts)} relevant fact(s) from FalkorDB / Graphiti:\n",
    ]

    for idx, item in enumerate(facts, 1):
        fact_text = item.get("fact", "Unknown fact")
        valid_at = item.get("valid_at", "N/A")
        invalid_at = item.get("invalid_at")
        episodes = item.get("episodes", [])

        source_eps = item.get("source_episodes") or []
        episode_labels = item.get("episode_names") or [str(e) for e in episodes]

        status_tag = "ACTIVE" if not invalid_at else f"SUPERSEDED (invalidated at {invalid_at})"
        lines.append(f"#### {idx}. {fact_text}")
        lines.append(f"- **Status:** `{status_tag}`")
        lines.append(f"- **Valid From:** `{valid_at}`")
        if invalid_at:
            lines.append(f"- **Superseded At:** `{invalid_at}`")
        if episode_labels:
            lines.append(f"- **Source Episodes:** `{', '.join(episode_labels)}`")
        for se in source_eps:
            if not se.get("content"):
                continue
            prov = f"  _({se['provenance']})_" if se.get("provenance") else ""
            lines.append(f"  > {se['content']}{prov}")
        lines.append("")

    return "\n".join(lines).strip()


@overload
async def recall_mem(
    query: str,
    max_results: int = 10,
    format_for_mcp: Literal[True] = True,
    max_retries: int = 3,
) -> str: ...


@overload
async def recall_mem(
    query: str,
    max_results: int = 10,
    format_for_mcp: Literal[False] = ...,
    max_retries: int = 3,
) -> list[dict[str, Any]]: ...


@overload
async def recall_mem(
    query: str,
    max_results: int = 10,
    format_for_mcp: bool = ...,
    max_retries: int = 3,
) -> str | list[dict[str, Any]]: ...


async def recall_mem(
    query: str,
    max_results: int = 10,
    format_for_mcp: bool = True,
    max_retries: int = 3,
) -> str | list[dict[str, Any]]:
    """Search episodic memory (Graphiti + FalkorDB) for facts related to query.

    Args:
        query: Search query for relevant facts.
        max_results: Maximum facts to return.
        format_for_mcp: If True, return formatted Markdown string.
        max_retries: Number of retries on transient rate limits (429).

    Raises:
        GeminiQuotaExhaustedError: if every model in the configured free-tier
            chain lacks headroom right now (search() also calls the LLM for
            reranking). No Gemini call is made in this case.
    """
    graphiti, chosen_model = get_graphiti_for_operation()
    logger.info(f"Recalling episodic memory for query: '{_log_query(query)}' (model: {chosen_model})")
    call_start = time.monotonic()
    embed_before, comp_before = _call_counts()

    results = []
    for attempt in range(max_retries):
        try:
            results = await graphiti.search(neutralize_fulltext_hazards(query))
            break
        except Exception as e:
            if _is_transient_gemini_error(e) and attempt < max_retries - 1:
                backoff = 5.0 * (attempt + 1)
                logger.warning(
                    f"Transient provider error in recall_mem() ({_classify_transient_error(e)}). "
                    f"Retrying in {backoff:.1f}s (attempt {attempt+1}/{max_retries})..."
                )
                await asyncio.sleep(backoff)
            else:
                raise

    embed_after, comp_after = _call_counts()
    logger.info(
        f"Recalled '{_log_query(query)}': {embed_after - embed_before} embedding + "
        f"{comp_after - comp_before} completion call(s) in {time.monotonic() - call_start:.1f}s "
        f"({len(results)} raw result(s))"
    )

    edge_facts: list[dict[str, Any]] = []
    for edge in results[: max(max_results, 10)]:
        fact_data = {
            "fact": getattr(edge, "fact", str(edge)),
            "valid_at": getattr(edge, "valid_at", None),
            "invalid_at": getattr(edge, "invalid_at", None),
            "episodes": getattr(edge, "episodes", []),
            "created_at": getattr(edge, "created_at", None),
        }
        if isinstance(fact_data["valid_at"], datetime):
            fact_data["valid_at"] = fact_data["valid_at"].isoformat()
        if isinstance(fact_data["invalid_at"], datetime):
            fact_data["invalid_at"] = fact_data["invalid_at"].isoformat()
        if isinstance(fact_data["created_at"], datetime):
            fact_data["created_at"] = fact_data["created_at"].isoformat()

        edge_facts.append(fact_data)

    # Second arm: KNN over the episode's synthesized statement itself. Reaches
    # decisions the RELATES_TO edge search misses — no edge extracted, or the
    # extracted edges don't carry the query's terms. No-op on a graph without
    # the vector index. RRF-fused with the edge results, deduped by fact text.
    # Cap the vector arm at its high-confidence head — its rank 7+ hits are
    # semantic neighbours, not real matches, and they add noise the consumer
    # then weaves in (e.g. an unrelated storage episode read as a "backend").
    vector_facts = await _episode_vector_search(graphiti, query, min(max_results, _EPISODE_VECTOR_K))
    facts = _rrf_merge(edge_facts, vector_facts, limit=max_results)

    # Attach each fact's source-episode synthesized statement + provenance so a
    # consumer sees the reasoning behind the terse RELATES_TO edge, not just the
    # edge. One batched read; degrades to bare facts on failure.
    ep_index = await _resolve_episode_index(
        graphiti, [u for f in facts for u in (f.get("episodes") or [])]
    )
    for f in facts:
        srcs = [ep_index[u] for u in (f.get("episodes") or []) if u in ep_index]
        f["source_episodes"] = srcs
        if srcs:
            f["episode_names"] = [s["name"] for s in srcs]

    if format_for_mcp:
        return format_memory_results_for_mcp(facts, query)
    return facts


def parse_iso_datetime(date_input: str | datetime) -> datetime:
    """Parse various date string formats into a timezone-aware UTC datetime.

    Supports ISO-8601 strings (e.g. '2025-01-13T00:00:00Z'), standard dates ('2025-01-13'),
    and other common formats.
    """
    if isinstance(date_input, datetime):
        if date_input.tzinfo is None:
            return date_input.replace(tzinfo=timezone.utc)
        return date_input.astimezone(timezone.utc)

    s = date_input.strip()
    # YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        dt = datetime.strptime(s, "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)

    # ISO formats with Z or offset
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass

    # Common standard fallbacks
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%B %d, %Y"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass

    raise ValueError(
        f"Unable to parse '{date_input}' into a valid date/time format. Please use 'YYYY-MM-DD' or ISO-8601."
    )


def format_edit_memory_results_for_mcp(
    target_query: str,
    dry_run: bool,
    matched_episodes: list[dict[str, Any]],
    matched_entities: list[dict[str, Any]],
    matched_edges: list[dict[str, Any]],
    registry_updates: list[dict[str, Any]],
    reextraction: Optional[dict[str, Any]] = None,
) -> str:
    """Format memory edit/update results into clean Markdown for MCP clients."""
    status_label = "DRY RUN (Preview Only - No Changes Made)" if dry_run else "COMMITTED (Graphiti and FalkorDB Updated)"

    if not matched_episodes and not matched_entities and not matched_edges:
        return f"### ⚠️ Memory Edit Report ({status_label})\n\nNo matching episodes, entities, or facts found for query: `{target_query}`."

    lines = [
        f"### ✏️ Episodic Memory Edit Report ({status_label})",
        f"- **Target Query:** `{target_query}`",
        f"- **Mode:** `{'dry_run' if dry_run else 'commit'}`",
        f"- **Matched Episodes:** {len(matched_episodes)}",
        f"- **Matched Entities:** {len(matched_entities)}",
        f"- **Matched Graph Relationships:** {len(matched_edges)}",
        f"- **Import Registry Synchronizations:** {len(registry_updates)}",
        "",
    ]

    if matched_episodes:
        lines.append("#### 🎬 Episode Nodes:")
        for ep in matched_episodes:
            ep_display_name = ep.get("new_name") or ep.get("old_name") or ep.get("name") or "Episode"
            lines.append(f"**Episode:** `{ep_display_name}` (`{ep.get('uuid')}`)")
            if ep.get("valid_at_changed"):
                lines.append(f"  - **Valid From / Reference Time:** `{ep.get('old_valid_at')}` ➔ `{ep.get('new_valid_at')}`")
            if ep.get("content_changed"):
                lines.append(f"  - **Old Content:** {ep.get('old_content')}")
                lines.append(f"  - **New Content:** {ep.get('new_content')}")
            if ep.get("name_changed"):
                lines.append(f"  - **Old Name:** `{ep.get('old_name')}` ➔ `{ep.get('new_name')}`")
            lines.append("")

    if matched_entities:
        lines.append("#### 🏷️ Entity Nodes:")
        for ent in matched_entities:
            lines.append(f"**Entity:** `{ent.get('name')}` (`{ent.get('uuid')}`)")
            if ent.get("summary_changed"):
                lines.append(f"  - **Old Summary:** {ent.get('old_summary')}")
                lines.append(f"  - **New Summary:** {ent.get('new_summary')}")
            lines.append("")

    if matched_edges:
        lines.append("#### 🔗 Graph Edges:")
        for edge in matched_edges:
            lines.append(f"**Edge Fact:** {edge.get('fact', 'N/A')}")
            if edge.get("valid_at_changed"):
                lines.append(f"  - **Valid From:** `{edge.get('old_valid_at')}` ➔ `{edge.get('new_valid_at')}`")
            lines.append("")

    if registry_updates:
        lines.append("#### 📋 Import Registry Synchronizations:")
        for reg in registry_updates:
            lines.append(f"- **Candidate:** `{reg.get('candidate_id')}` (`{reg.get('fingerprint')}`)")
            lines.append(f"  - **Reference Time:** `{reg.get('old_reference_time')}` ➔ `{reg.get('new_reference_time')}`")
            lines.append(f"  - **Episode Name:** `{reg.get('old_episode_name')}` ➔ `{reg.get('new_episode_name')}`")
            lines.append(f"  - **Text:** `{reg.get('new_text')}`")
            lines.append("")

    if reextraction:
        lines.append(f"#### ♻️ Fact Re-extraction: `{reextraction.get('status')}`")
        retract = reextraction.get("retract") or {}
        for f in retract.get("facts_deleted", []):
            lines.append(f"- **Removed fact:** {f.get('fact')}")
        for f in retract.get("facts_detached", []):
            lines.append(f"- **Detached (still supported elsewhere):** {f.get('fact')}")
        for fact in reextraction.get("new_facts", []):
            lines.append(f"- **New fact:** {fact}")
        if reextraction.get("message"):
            lines.append(f"- {reextraction['message']}")
        lines.append("")

    return "\n".join(lines).strip()


_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EDIT_LEDGER = _REPO_ROOT / "imports" / "state" / "edit_memory_ledger.jsonl"


def default_import_registry_path() -> Path:
    """imports/state/import_registry.json at the repo root, or CMF_IMPORT_REGISTRY.

    Until 2026-10-06 edit_memory and reconcile_memories resolved this one
    directory too shallow (server/imports/...), a path that never existed,
    so registry sync was silently skipped. Tests point CMF_IMPORT_REGISTRY
    at a temp file (tests/conftest.py) so they can't write the real one.
    """
    override = os.getenv("CMF_IMPORT_REGISTRY")
    return Path(override) if override else _REPO_ROOT / "imports" / "state" / "import_registry.json"


def _append_edit_ledger(ledger_path: Path, record: dict[str, Any]) -> None:
    """Append one edit_memory re-extraction record (best-effort, JSONL)."""
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), **record}, default=str) + "\n")
    except Exception as e:  # noqa: BLE001 - the ledger must never block an edit
        logger.warning(f"edit_memory ledger write failed: {e}")


async def _reextract_episode(
    graphiti: Graphiti,
    old_uuid: str,
    *,
    content: str,
    name: str,
    reference_time: str | datetime,
) -> dict[str, Any]:
    """Replace one episode with a fresh extraction of `content`, keeping its name.

    Add first, retract second: the new episode is extracted under a temporary
    name, post-processed and tagged exactly as remember() does, and only then
    is the old episode retracted (episode_retract) and the new one renamed
    back. A failed extraction removes the partial new episode and leaves the
    old one untouched. A fact the new extraction repeats gets the new episode
    added to `r.episodes` by Graphiti's edge dedupe before the old episode is
    detached, so it survives.

    add_episode is called directly, not through remember(), because
    resolve_remember_identity renames any name not shaped
    <harness>-<project>-NNN.
    """
    from uuid import uuid4

    from server.consolidation.graph_tagging import HARNESS_LABELS, tag_promoted_episode
    from server.providers.episode_retract import retract_episode

    driver = graphiti.driver
    rows = await driver.execute_query(
        "MATCH (e:Episodic {uuid: $u}) RETURN e.source_description AS sd, e.project AS project, labels(e) AS labels",
        u=old_uuid,
    )
    rows = rows[0] if rows and isinstance(rows[0], list) else (rows or [])
    if not rows:
        raise ValueError(f"episode {old_uuid} no longer exists; nothing re-extracted")
    sd = rows[0]["sd"] or "Context Memory Fabric MCP"
    project = rows[0]["project"]
    labels = rows[0]["labels"] or []
    harness = next((h for h, label in HARNESS_LABELS.items() if label in labels), None)

    pending = f"{name}__edit-pending-{uuid4().hex[:8]}"
    new_uuid: Optional[str] = None
    try:
        result = await graphiti.add_episode(
            name=pending,
            episode_body=neutralize_fulltext_hazards(content),
            source_description=sd,
            reference_time=parse_iso_datetime(reference_time),
            source=EpisodeType.text,
            **extraction_kwargs(source_description=sd),
        )
        new_uuid = result.episode.uuid
        await postprocess_episode(graphiti, result, episode_name=pending, content=content, source_description=sd)
        await tag_promoted_episode(driver, pending, project, harness)
    except BaseException:
        leftovers = await driver.execute_query("MATCH (e:Episodic {name: $n}) RETURN e.uuid AS uuid", n=pending)
        leftovers = leftovers[0] if leftovers and isinstance(leftovers[0], list) else (leftovers or [])
        for r in leftovers:
            await retract_episode(driver, r["uuid"])
        raise

    retracted = await retract_episode(driver, old_uuid)
    await driver.execute_query("MATCH (e:Episodic {uuid: $u}) SET e.name = $name", u=new_uuid, name=name)

    facts = await driver.execute_query(
        "MATCH ()-[r:RELATES_TO]->() WHERE $u IN r.episodes RETURN r.fact AS fact", u=new_uuid,
    )
    facts = facts[0] if facts and isinstance(facts[0], list) else (facts or [])
    return {
        "status": "done",
        "name": name,
        "old_uuid": old_uuid,
        "new_uuid": new_uuid,
        "retract": retracted,
        "new_facts": [f["fact"] for f in facts],
    }


@overload
async def edit_memory(
    target_query: str,
    new_reference_time: Optional[str | datetime] = None,
    new_content: Optional[str] = None,
    new_summary: Optional[str] = None,
    new_name: Optional[str] = None,
    dry_run: bool = False,
    format_for_mcp: Literal[True] = True,
    registry_path: Optional[str | Path] = None,
    background: bool = False,
    ledger_path: Optional[str | Path] = None,
    journal_db: Optional[str | Path] = None,
) -> str: ...


@overload
async def edit_memory(
    target_query: str,
    new_reference_time: Optional[str | datetime] = None,
    new_content: Optional[str] = None,
    new_summary: Optional[str] = None,
    new_name: Optional[str] = None,
    dry_run: bool = False,
    format_for_mcp: Literal[False] = ...,
    registry_path: Optional[str | Path] = None,
    background: bool = False,
    ledger_path: Optional[str | Path] = None,
    journal_db: Optional[str | Path] = None,
) -> dict[str, Any]: ...


@overload
async def edit_memory(
    target_query: str,
    new_reference_time: Optional[str | datetime] = None,
    new_content: Optional[str] = None,
    new_summary: Optional[str] = None,
    new_name: Optional[str] = None,
    dry_run: bool = False,
    format_for_mcp: bool = ...,
    registry_path: Optional[str | Path] = None,
    background: bool = False,
    ledger_path: Optional[str | Path] = None,
    journal_db: Optional[str | Path] = None,
) -> str | dict[str, Any]: ...


async def edit_memory(
    target_query: str,
    new_reference_time: Optional[str | datetime] = None,
    new_content: Optional[str] = None,
    new_summary: Optional[str] = None,
    new_name: Optional[str] = None,
    dry_run: bool = False,
    format_for_mcp: bool = True,
    registry_path: Optional[str | Path] = None,
    background: bool = False,
    ledger_path: Optional[str | Path] = None,
    journal_db: Optional[str | Path] = None,
) -> str | dict[str, Any]:
    """Edit, correct, or re-date existing episodic memory episodes, entities, and edges in FalkorDB.

    Writes are scoped to whatever `target_query` directly matches (by
    name/content/summary substring or exact uuid) — never to nodes only
    reached by walking MENTIONS from a match (an entity co-occurring in the
    same episode, an episode mentioning the same entity). `matched_entities`
    / `matched_episodes` in the result still include that wider connected
    context for display, but only nodes in the direct match set can appear
    in `modified_entities` / `modified_episodes`. A broad `target_query`
    that directly matches several nodes still edits all of them — narrow
    the query (prefer an exact uuid) to scope a correction to one node.

    Args:
        target_query: Search string, entity name, episode name, or UUID identifying the memory to edit.
        new_reference_time: New date/timestamp for the episode (e.g. '2025-01-13' or '2025-01-13T00:00:00Z').
        new_content: Optional updated body content for the episode.
        new_summary: Optional updated summary for matched entity node(s).
        new_name: Optional updated identifier name for the episode or entity.
        dry_run: If True, previews changes without writing to FalkorDB or updating the import registry.
        format_for_mcp: If True, returns formatted Markdown for MCP responses.
        registry_path: Path to import registry JSON (defaults to project-root imports/state/import_registry.json).
        background: Run a `new_content` re-extraction as a background task and return at once
            (the MCP tool does this; extraction takes ~30-40s on Spark).
        ledger_path: JSONL ledger for re-extractions (default imports/state/edit_memory_ledger.jsonl).
        journal_db: Journal path whose directory holds the Spark lock (default: the live journal).

    `new_content` replaces the episode's facts as well as its text: the
    episode is re-extracted from the new content and the old episode's facts,
    mentions and orphaned entities are retracted (_reextract_episode). That
    needs the Spark slot and exactly one directly-matched episode; otherwise
    it raises before writing anything. Date-only edits (the automatic date
    substitution in content) keep the no-LLM path.
    """
    if not target_query or not target_query.strip():
        raise ValueError("target_query cannot be empty.")

    clean_query = target_query.strip()
    graphiti = get_graphiti()
    driver = graphiti.driver

    # 1. Parse new reference time if provided
    new_dt: Optional[datetime] = None
    new_valid_at_iso: Optional[str] = None
    new_date_str: Optional[str] = None
    new_compact_str: Optional[str] = None

    if new_reference_time:
        new_dt = parse_iso_datetime(new_reference_time)
        new_valid_at_iso = new_dt.isoformat()
        new_date_str = new_dt.strftime("%Y-%m-%d")
        new_compact_str = new_dt.strftime("%Y%m%d")

    # 2. Locate matching Entities and Episodes in FalkorDB
    entities_map: dict[str, dict[str, Any]] = {}
    episodes_map: dict[str, dict[str, Any]] = {}

    # Query Entity nodes
    cypher_entities = (
        "MATCH (n:Entity) "
        "WHERE toLower(n.name) CONTAINS toLower($query) "
        "   OR toLower(n.summary) CONTAINS toLower($query) "
        "   OR n.uuid = $query "
        "RETURN n.uuid AS uuid, n.name AS name, n.summary AS summary"
    )
    ent_rows = await driver.execute_query(cypher_entities, query=clean_query)
    if ent_rows and len(ent_rows) > 0 and isinstance(ent_rows[0], list):
        for r in ent_rows[0]:
            entities_map[r["uuid"]] = dict(r)

    # Query Episode nodes
    cypher_episodes = (
        "MATCH (e:Episodic) "
        "WHERE toLower(e.name) CONTAINS toLower($query) "
        "   OR toLower(e.content) CONTAINS toLower($query) "
        "   OR e.uuid = $query "
        "RETURN e.uuid AS uuid, e.name AS name, e.valid_at AS valid_at, e.content AS content"
    )
    ep_rows = await driver.execute_query(cypher_episodes, query=clean_query)
    if ep_rows and len(ep_rows) > 0 and isinstance(ep_rows[0], list):
        for r in ep_rows[0]:
            episodes_map[r["uuid"]] = dict(r)

    # Nodes `target_query` actually matched — writes are scoped to these,
    # never to the connected context pulled in below. Before this fix, a
    # query matching one entity (e.g. by exact uuid) would still write
    # `new_summary`/`new_content`/`new_name`/`new_reference_time` to every
    # OTHER entity/episode that happens to co-occur with it in some episode
    # — MS6b's entity-audit dry-run caught this: targeting a single entity
    # by uuid matched 6 entities for a `new_summary` write. The expanded
    # `entities_map`/`episodes_map` below is still built and returned
    # (`matched_entities`/`matched_episodes`) as read-only context — useful
    # for seeing what's connected — it is just never mutated.
    direct_entity_uuids = set(entities_map.keys())
    direct_episode_uuids = set(episodes_map.keys())

    # For each matched Entity, find connected Episodes
    for ent_uuid in list(entities_map.keys()):
        cypher_conn_ep = (
            "MATCH (e:Episodic)-[:MENTIONS]->(n:Entity) "
            "WHERE n.uuid = $uuid "
            "RETURN e.uuid AS uuid, e.name AS name, e.valid_at AS valid_at, e.content AS content"
        )
        conn_eps = await driver.execute_query(cypher_conn_ep, uuid=ent_uuid)
        if conn_eps and len(conn_eps) > 0 and isinstance(conn_eps[0], list):
            for r in conn_eps[0]:
                if r["uuid"] not in episodes_map:
                    episodes_map[r["uuid"]] = dict(r)

    # For each matched Episode, find connected Entities
    for ep_uuid in list(episodes_map.keys()):
        cypher_conn_ent = (
            "MATCH (e:Episodic)-[:MENTIONS]->(n:Entity) "
            "WHERE e.uuid = $uuid "
            "RETURN n.uuid AS uuid, n.name AS name, n.summary AS summary"
        )
        conn_ents = await driver.execute_query(cypher_conn_ent, uuid=ep_uuid)
        if conn_ents and len(conn_ents) > 0 and isinstance(conn_ents[0], list):
            for r in conn_ents[0]:
                if r["uuid"] not in entities_map:
                    entities_map[r["uuid"]] = dict(r)

    # 3. Find connected Edges. Same direct-vs-connected split as entities/
    # episodes above: edges_map (built from the full connected episode set)
    # is display-only context; direct_edge_uuids (built from directly-
    # matched episodes only) is what re-dating is actually allowed to touch
    # — an edge belonging to a bystander episode pulled in only because it
    # shares an entity with the real target must not get re-dated too.
    edges_map: dict[str, dict[str, Any]] = {}
    direct_edge_uuids: set[str] = set()
    for ep_uuid in episodes_map.keys():
        cypher_edge = (
            "MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity) "
            "WHERE $ep_uuid IN r.episodes "
            "RETURN r.uuid AS uuid, r.name AS name, r.fact AS fact, r.valid_at AS valid_at, r.invalid_at AS invalid_at, r.episodes AS episodes"
        )
        edge_rows = await driver.execute_query(cypher_edge, ep_uuid=ep_uuid)
        if edge_rows and len(edge_rows) > 0 and isinstance(edge_rows[0], list):
            for r in edge_rows[0]:
                edges_map[r["uuid"]] = dict(r)
                if ep_uuid in direct_episode_uuids:
                    direct_edge_uuids.add(r["uuid"])

    # 3b. A new_content edit re-extracts the episode's facts on Spark. Check
    # everything that can refuse it before any write below.
    reextract_uuid: Optional[str] = None
    if new_content is not None and direct_episode_uuids:
        if len(direct_episode_uuids) > 1:
            names = sorted(episodes_map[u].get("name") or u for u in direct_episode_uuids)
            raise ValueError(
                f"new_content would replace {len(direct_episode_uuids)} episodes ({', '.join(names[:5])}); "
                "target exactly one episode, preferably by uuid. Nothing was changed."
            )
        (only_uuid,) = direct_episode_uuids
        if (episodes_map[only_uuid].get("content") or "") != new_content:
            reextract_uuid = only_uuid

    spark_hold = contextlib.ExitStack()
    if reextract_uuid and not dry_run:
        from server.adapters.spark_lock import spark_slot

        busy = spark_hold.enter_context(spark_slot(Path(journal_db) if journal_db else None))
        if busy:
            spark_hold.close()
            raise RuntimeError(
                f"Spark is busy ({busy}), and replacing an episode's content re-extracts its facts there. "
                "Nothing was changed; retry when the Spark job finishes."
            )
    # The slot is released when this function exits, unless the re-extraction
    # is handed to a background task, which then releases it itself.
    try:
        return await _edit_memory_writes(
            spark_hold, reextract_uuid, graphiti, driver, clean_query, episodes_map, entities_map, edges_map,
            direct_episode_uuids, direct_entity_uuids, direct_edge_uuids,
            new_valid_at_iso, new_date_str, new_compact_str, new_content, new_summary, new_name,
            dry_run, format_for_mcp, registry_path, background,
            Path(ledger_path) if ledger_path else DEFAULT_EDIT_LEDGER,
        )
    finally:
        spark_hold.close()


async def _edit_memory_writes(
    spark_hold: contextlib.ExitStack,
    reextract_uuid: Optional[str],
    graphiti: Graphiti,
    driver: Any,
    clean_query: str,
    episodes_map: dict[str, dict[str, Any]],
    entities_map: dict[str, dict[str, Any]],
    edges_map: dict[str, dict[str, Any]],
    direct_episode_uuids: set[str],
    direct_entity_uuids: set[str],
    direct_edge_uuids: set[str],
    new_valid_at_iso: Optional[str],
    new_date_str: Optional[str],
    new_compact_str: Optional[str],
    new_content: Optional[str],
    new_summary: Optional[str],
    new_name: Optional[str],
    dry_run: bool,
    format_for_mcp: bool,
    registry_path: Optional[str | Path],
    background: bool,
    ledger_path: Path,
) -> str | dict[str, Any]:
    """edit_memory's write half: steps 4-6 over the nodes it matched."""
    # 4. Plan and track modifications
    modified_episodes: list[dict[str, Any]] = []
    modified_entities: list[dict[str, Any]] = []
    modified_edges: list[dict[str, Any]] = []
    registry_updates: list[dict[str, Any]] = []

    # Extract old date string candidates from directly-matched episodes only
    # if re-dating — this only ever needs to know the dates being replaced
    # on the episodes actually being written to.
    old_date_strs: set[str] = set()
    old_compact_strs: set[str] = set()

    for ep_uuid in direct_episode_uuids:
        ep = episodes_map[ep_uuid]
        old_val = ep.get("valid_at")
        if old_val:
            d_match = re.search(r"(\d{4}-\d{2}-\d{2})", str(old_val))
            if d_match:
                old_date_strs.add(d_match.group(1))
                old_compact_strs.add(d_match.group(1).replace("-", ""))

    for ep_uuid in direct_episode_uuids:
        old_content = episodes_map[ep_uuid].get("content") or ""
        for m in re.finditer(r"\b(\d{4}-\d{2}-\d{2})\b", old_content):
            old_date_strs.add(m.group(1))
            old_compact_strs.add(m.group(1).replace("-", ""))

    # Process Episode modifications — scoped to directly-matched episodes only.
    for ep_uuid in direct_episode_uuids:
        ep = episodes_map[ep_uuid]
        old_valid_at = ep.get("valid_at")
        old_content = ep.get("content") or ""
        old_name = ep.get("name") or ""

        target_valid_at = old_valid_at
        target_content = old_content
        target_name = old_name

        valid_at_changed = False
        content_changed = False
        name_changed = False

        if new_valid_at_iso and old_valid_at != new_valid_at_iso:
            target_valid_at = new_valid_at_iso
            valid_at_changed = True

        if new_content is not None:
            if new_content != old_content:
                target_content = new_content
                content_changed = True
        elif new_date_str:
            # Auto-replace old date string in content if found
            for od in old_date_strs:
                if od in target_content:
                    target_content = target_content.replace(od, new_date_str)
                    content_changed = True

        if new_name is not None:
            if new_name != old_name:
                target_name = new_name
                name_changed = True
        elif new_compact_str:
            # Auto-replace compact date in episode name if found
            for oc in old_compact_strs:
                if oc in target_name:
                    target_name = target_name.replace(oc, new_compact_str)
                    name_changed = True
            if new_date_str:
                for od in old_date_strs:
                    if od in target_name:
                        target_name = target_name.replace(od, new_date_str)
                        name_changed = True

        if valid_at_changed or content_changed or name_changed:
            ep_change = {
                "uuid": ep_uuid,
                "old_name": old_name,
                "new_name": target_name,
                "name_changed": name_changed,
                "old_valid_at": old_valid_at,
                "new_valid_at": target_valid_at,
                "valid_at_changed": valid_at_changed,
                "old_content": old_content,
                "new_content": target_content,
                "content_changed": content_changed,
                "reextract": ep_uuid == reextract_uuid,
            }
            modified_episodes.append(ep_change)

            # A re-extracted episode is replaced as a whole at the end, not edited in place.
            if not dry_run and ep_uuid != reextract_uuid:
                await driver.execute_query(
                    "MATCH (e:Episodic {uuid: $uuid}) SET e.valid_at = $valid_at, e.content = $content, e.name = $name",
                    uuid=ep_uuid,
                    valid_at=target_valid_at,
                    content=target_content,
                    name=target_name,
                )

    # Process Entity modifications — scoped to directly-matched entities only.
    for ent_uuid in direct_entity_uuids:
        ent = entities_map[ent_uuid]
        old_summary = ent.get("summary") or ""
        target_summary = old_summary
        summary_changed = False

        if new_summary is not None:
            if new_summary != old_summary:
                target_summary = new_summary
                summary_changed = True
        elif new_date_str:
            for od in old_date_strs:
                if od in target_summary:
                    target_summary = target_summary.replace(od, new_date_str)
                    summary_changed = True

        if summary_changed:
            ent_change = {
                "uuid": ent_uuid,
                "name": ent.get("name"),
                "old_summary": old_summary,
                "new_summary": target_summary,
                "summary_changed": summary_changed,
            }
            modified_entities.append(ent_change)

            if not dry_run:
                await driver.execute_query(
                    "MATCH (n:Entity {uuid: $uuid}) SET n.summary = $summary",
                    uuid=ent_uuid,
                    summary=target_summary,
                )

    # Process Edge modifications — scoped to edges of directly-matched episodes
    # only. A re-extracted episode's edges are replaced, so none are re-dated.
    for edge_uuid in (set() if reextract_uuid else direct_edge_uuids):
        edge = edges_map[edge_uuid]
        old_valid_at = edge.get("valid_at")
        target_valid_at = old_valid_at
        valid_at_changed = False

        if new_valid_at_iso and old_valid_at != new_valid_at_iso:
            target_valid_at = new_valid_at_iso
            valid_at_changed = True

        if valid_at_changed:
            edge_change = {
                "uuid": edge_uuid,
                "fact": edge.get("fact"),
                "old_valid_at": old_valid_at,
                "new_valid_at": target_valid_at,
                "valid_at_changed": valid_at_changed,
            }
            modified_edges.append(edge_change)

            if not dry_run:
                await driver.execute_query(
                    "MATCH ()-[r {uuid: $uuid}]->() SET r.valid_at = $valid_at",
                    uuid=edge_uuid,
                    valid_at=target_valid_at,
                )

    # 5. Synchronize with import_registry.json if applicable
    actual_reg_path = Path(registry_path) if registry_path else default_import_registry_path()
    if actual_reg_path.exists():
        try:
            with open(actual_reg_path, "r", encoding="utf-8") as f:
                reg_data = json.load(f)
            records = reg_data.get("records", {})
            reg_modified = False

            for rec_key, rec in records.items():
                rec_ep_name = rec.get("episode_name")
                rec_text = rec.get("text", "")

                for ep_change in modified_episodes:
                    if rec_ep_name == ep_change["old_name"] or ep_change["old_content"] in rec_text:
                        old_ref = rec.get("reference_time")
                        new_ref = ep_change["new_valid_at"]
                        old_ep = rec.get("episode_name")
                        new_ep = ep_change["new_name"]
                        new_tx = ep_change["new_content"]

                        reg_info = {
                            "fingerprint": rec_key,
                            "candidate_id": rec.get("candidate_id"),
                            "old_reference_time": old_ref,
                            "new_reference_time": new_ref,
                            "old_episode_name": old_ep,
                            "new_episode_name": new_ep,
                            "new_text": new_tx,
                        }
                        registry_updates.append(reg_info)

                        if not dry_run:
                            rec["reference_time"] = new_ref
                            rec["episode_name"] = new_ep
                            rec["text"] = new_tx
                            reg_modified = True

            if reg_modified and not dry_run:
                with open(actual_reg_path, "w", encoding="utf-8") as f:
                    json.dump(reg_data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to synchronize import registry during edit_memory: {e}")

    # 6. Re-extract a content-edited episode: its facts follow its new text.
    reextraction: Optional[dict[str, Any]] = None
    if reextract_uuid:
        from server.providers.episode_retract import retract_episode

        ep_change = next(c for c in modified_episodes if c["uuid"] == reextract_uuid)
        if dry_run:
            reextraction = {
                "status": "dry_run",
                "retract": await retract_episode(driver, reextract_uuid, apply=False),
                "message": "On commit, the episode is re-extracted from the new content on Spark (~30-40s).",
            }
        else:
            reextraction = await _start_reextraction(
                spark_hold.pop_all(), ep_change, background=background, ledger_path=ledger_path,
            )

    result_data = {
        "target_query": clean_query,
        "dry_run": dry_run,
        "matched_episodes": modified_episodes,
        "matched_entities": modified_entities,
        "matched_edges": modified_edges,
        "registry_updates": registry_updates,
        "reextraction": reextraction,
    }

    if format_for_mcp:
        return format_edit_memory_results_for_mcp(
            target_query=clean_query,
            dry_run=dry_run,
            matched_episodes=modified_episodes,
            matched_entities=modified_entities,
            matched_edges=modified_edges,
            registry_updates=registry_updates,
            reextraction=reextraction,
        )
    return result_data


_BACKGROUND_EDIT_TASKS: set[asyncio.Task] = set()


async def _start_reextraction(
    spark_hold: contextlib.ExitStack,
    ep_change: dict[str, Any],
    *,
    background: bool,
    ledger_path: Path,
) -> dict[str, Any]:
    """Run (or queue) _reextract_episode for one edit; owns and releases the Spark slot."""
    old_uuid, name = ep_change["uuid"], ep_change["new_name"]
    _append_edit_ledger(ledger_path, {
        "event": "started", "old_uuid": old_uuid, "old_name": ep_change["old_name"], "name": name,
        "old_valid_at": ep_change["old_valid_at"], "new_valid_at": ep_change["new_valid_at"],
        "old_content": ep_change["old_content"], "new_content": ep_change["new_content"],
    })

    async def _run() -> dict[str, Any]:
        with spark_hold:
            try:
                graphiti, _ = get_graphiti_for_operation()
                outcome = await _reextract_episode(
                    graphiti, old_uuid, content=ep_change["new_content"], name=name,
                    reference_time=ep_change["new_valid_at"] or datetime.now(timezone.utc),
                )
            except Exception as e:
                _append_edit_ledger(ledger_path, {"event": "failed", "old_uuid": old_uuid, "name": name, "error": repr(e)})
                raise
        _append_edit_ledger(ledger_path, {"event": "done", **outcome})
        return outcome

    if not background:
        return await _run()

    async def _logged() -> None:
        try:
            await _run()
            logger.info(f"edit_memory re-extraction committed: '{name}'")
        except Exception:
            logger.exception(f"edit_memory re-extraction FAILED (old episode kept): '{name}'")

    task = asyncio.create_task(_logged())
    _BACKGROUND_EDIT_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_EDIT_TASKS.discard)
    return {
        "status": "queued",
        "name": name,
        "old_uuid": old_uuid,
        "message": (
            "Re-extracting facts from the new content in the background (~30-40s on Spark). "
            "Until it finishes the episode keeps its old text and facts; recall_mem afterwards to confirm."
        ),
    }


def format_reconcile_results_for_mcp(
    created: list[dict[str, Any]],
    updated: list[dict[str, Any]],
    consolidated: list[dict[str, Any]],
    discarded: list[dict[str, Any]],
    dry_run: bool = False,
    errors: int = 0,
) -> str:
    """Format memory reconciliation results into structured Markdown for MCP."""
    status_str = "DRY RUN (No Graphiti writes)" if dry_run else "COMMITTED (Graphiti/FalkorDB updated)"
    lines = [
        f"### 🔄 Historical Memory Reconciliation Report ({status_str})\n",
        f"- **Episodes Created:** {len(created)}",
        f"- **Episodes Updated / Upserted:** {len(updated)}",
        f"- **Candidates Consolidated:** {len(consolidated)}",
        f"- **Candidates Discarded / Rejected:** {len(discarded)}",
        f"- **Errors:** {errors}",
        "",
    ]

    if created:
        lines.append("#### 🆕 Episodes Created:")
        for ep in created:
            lines.append(f"- **{ep.get('name')}** (Date: `{ep.get('event_date')}`, Precision: `{ep.get('event_date_precision', 'day')}`)")
            lines.append(f"  - **Content:** {ep.get('content')}")
            if ep.get("observed_at"):
                lines.append(f"  - **Observed At:** `{ep.get('observed_at')}`")
            if ep.get("candidate_ids"):
                lines.append(f"  - **Candidates:** {', '.join(ep.get('candidate_ids', []))}")
        lines.append("")

    if updated:
        lines.append("#### ✏️ Episodes Updated / Upserted:")
        for ep in updated:
            lines.append(f"- **{ep.get('name')}** (Date: `{ep.get('event_date')}`)")
            lines.append(f"  - **Content:** {ep.get('content')}")
            if ep.get("observed_at"):
                lines.append(f"  - **Observed At:** `{ep.get('observed_at')}`")
            if ep.get("candidate_ids"):
                lines.append(f"  - **Candidates:** {', '.join(ep.get('candidate_ids', []))}")
        lines.append("")

    if consolidated:
        lines.append("#### 🔗 Candidates Consolidated:")
        for c in consolidated:
            lines.append(f"- **{c.get('name')}** (Consolidated: {', '.join(c.get('candidate_ids', []))})")
        lines.append("")

    if discarded:
        lines.append("#### 🚫 Candidates Discarded / Rejected:")
        for d in discarded:
            lines.append(f"- **{', '.join(d.get('candidate_ids', []))}**: {d.get('reason')}")
        lines.append("")

    return "\n".join(lines).strip()


@overload
async def reconcile_memories(
    records: list[dict[str, Any]],
    dry_run: bool = False,
    format_for_mcp: Literal[True] = True,
    registry_path: Optional[Path] = None,
) -> str: ...


@overload
async def reconcile_memories(
    records: list[dict[str, Any]],
    dry_run: bool = False,
    format_for_mcp: Literal[False] = ...,
    registry_path: Optional[Path] = None,
) -> dict[str, Any]: ...


@overload
async def reconcile_memories(
    records: list[dict[str, Any]],
    dry_run: bool = False,
    format_for_mcp: bool = ...,
    registry_path: Optional[Path] = None,
) -> str | dict[str, Any]: ...


async def reconcile_memories(
    records: list[dict[str, Any]],
    dry_run: bool = False,
    format_for_mcp: bool = True,
    registry_path: Optional[Path] = None,
) -> Union[str, dict[str, Any]]:
    """Reconcile and upsert episodic memories with real upsert/reject semantics.

    Args:
        records: List of structured reconciliation candidate records.
        dry_run: If True, previews changes without writing to Graphiti/FalkorDB.
        format_for_mcp: If True, returns formatted Markdown for MCP responses.
        registry_path: Optional path to import_registry.json.
    """
    if not records:
        if format_for_mcp:
            return "No reconciliation records provided."
        return {"created": [], "updated": [], "consolidated": [], "discarded": [], "errors": 0}

    graphiti = get_graphiti()
    driver = graphiti.driver
    actual_reg_path = Path(registry_path) if registry_path else default_import_registry_path()

    reg_data: dict[str, Any] = {"version": "1.0", "records": {}, "rejected_records": {}}
    if actual_reg_path.exists():
        try:
            with open(actual_reg_path, "r", encoding="utf-8") as f:
                reg_data = json.load(f)
        except Exception as e:
            logger.warning(f"Could not load existing registry: {e}")

    created: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    consolidated: list[dict[str, Any]] = []
    discarded: list[dict[str, Any]] = []
    errors = 0

    for rec in records:
        action = rec.get("action", "upsert_episode").lower()
        candidate_ids = rec.get("candidate_ids", [])
        if isinstance(candidate_ids, str):
            candidate_ids = [candidate_ids]

        # Case 1: Discard / Reject
        if action in ["discard_candidate", "reject_candidate", "reject", "discard"]:
            reason = rec.get("reason", "False or conflated memory.")
            notes = rec.get("notes", "")
            for cid in candidate_ids:
                reg_data.setdefault("rejected_records", {})[cid] = {
                    "candidate_id": cid,
                    "reason": reason,
                    "notes": notes,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            discarded.append({"candidate_ids": candidate_ids, "reason": reason, "notes": notes})

            if not dry_run:
                # If any matching episode exists, remove it
                for cid in candidate_ids:
                    try:
                        await driver.execute_query(
                            "MATCH (e:Episodic) WHERE toLower(e.name) CONTAINS toLower($cid) OR toLower(e.content) CONTAINS toLower($cid) DETACH DELETE e",
                            cid=cid,
                        )
                    except Exception as e:
                        logger.warning(f"Error purging rejected candidate {cid}: {e}")
            continue

        # Case 2: Upsert / Consolidate
        content = rec.get("content", "").strip()
        name = rec.get("name", "").strip()
        event_date_str = rec.get("event_date", "").strip()
        precision = rec.get("event_date_precision", "day").lower()
        observed_at = rec.get("observed_at")
        valid_from = rec.get("valid_from")
        valid_to = rec.get("valid_to")
        entities = rec.get("entities", [])
        notes = rec.get("notes", "")
        source = rec.get("source", "chatgpt").lower()

        if not content or not event_date_str:
            errors += 1
            logger.error(f"Record missing content or event_date: {rec}")
            continue

        # Parse event date
        try:
            if re.match(r"^\d{4}-\d{2}$", event_date_str):
                parts = event_date_str.split("-")
                event_dt = datetime(int(parts[0]), int(parts[1]), 1, tzinfo=timezone.utc)
                if precision == "day":
                    precision = "month"
            elif re.match(r"^\d{4}$", event_date_str):
                event_dt = datetime(int(event_date_str), 1, 1, tzinfo=timezone.utc)
                precision = "year"
            else:
                event_dt = parse_iso_datetime(event_date_str)
        except Exception as e:
            errors += 1
            logger.error(f"Failed to parse event_date '{event_date_str}': {e}")
            continue

        raw_fp = f"{source}:{content.lower()}:{event_dt.strftime('%Y%m%d')}"
        fp = hashlib.sha256(raw_fp.encode("utf-8")).hexdigest()[:8]
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", name.lower()).strip("_")[:30] if name else "episode"
        episode_name = f"reconciled_{event_dt.strftime('%Y%m%d')}_{slug}_{fp}"

        # Check if matching episode exists in FalkorDB
        matching_ep_uuid = None
        # 1. Search by candidate IDs or origin IDs in existing registry records
        for reg_k, reg_v in reg_data.get("records", {}).items():
            reg_cand_ids = reg_v.get("candidate_ids", [])
            if isinstance(reg_cand_ids, str):
                reg_cand_ids = [reg_cand_ids]
            if reg_v.get("candidate_id"):
                reg_cand_ids.append(reg_v.get("candidate_id"))
            if reg_v.get("origin_id"):
                reg_cand_ids.append(reg_v.get("origin_id"))

            if any(cid in reg_cand_ids for cid in candidate_ids):
                ep_n = reg_v.get("episode_name")
                if ep_n:
                    ep_check = await driver.execute_query(
                        "MATCH (e:Episodic) WHERE e.name = $ep_n RETURN e.uuid AS uuid",
                        ep_n=ep_n,
                    )
                    if ep_check and ep_check[0] and len(ep_check[0]) > 0:
                        matching_ep_uuid = ep_check[0][0]["uuid"]
                        break

        # 2. Search by content snippet and valid_at in FalkorDB
        if not matching_ep_uuid:
            snip = content[:40].lower()
            ep_check = await driver.execute_query(
                "MATCH (e:Episodic) WHERE e.valid_at STARTS WITH $dt_prefix AND toLower(e.content) CONTAINS $snip RETURN e.uuid AS uuid",
                dt_prefix=event_dt.strftime("%Y-%m"),
                snip=snip,
            )
            if ep_check and ep_check[0] and len(ep_check[0]) > 0:
                matching_ep_uuid = ep_check[0][0]["uuid"]

        item_info = {
            "name": name or episode_name,
            "episode_name": episode_name,
            "content": content,
            "event_date": event_date_str,
            "event_date_precision": precision,
            "observed_at": observed_at,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "candidate_ids": candidate_ids,
            "entities": entities,
            "notes": notes,
        }

        if matching_ep_uuid:
            # Update/replace existing episode in FalkorDB using Graphiti native removal + re-ingest
            updated.append(item_info)
            if not dry_run:
                try:
                    await graphiti.remove_episode(matching_ep_uuid)
                    desc = f"Reconciled {source.upper()} historical memory: {name}" if name else f"Reconciled {source.upper()} historical memory"
                    if notes:
                        desc += f" ({notes})"
                    await remember(
                        content=content,
                        name=episode_name,
                        source_description=desc,
                        reference_time=event_dt,
                    )
                    await asyncio.sleep(3.5)
                except Exception as e:
                    errors += 1
                    logger.error(f"Failed to replace episode {matching_ep_uuid}: {e}")
        else:
            # Create new episode via Graphiti
            created.append(item_info)
            if not dry_run:
                try:
                    desc = f"Reconciled {source.upper()} historical memory: {name}" if name else f"Reconciled {source.upper()} historical memory"
                    if notes:
                        desc += f" ({notes})"
                    await remember(
                        content=content,
                        name=episode_name,
                        source_description=desc,
                        reference_time=event_dt,
                    )
                    # Polite sleep between Graphiti ingest calls to respect LLM rate limits
                    await asyncio.sleep(3.5)
                except Exception as e:
                    errors += 1
                    logger.error(f"Failed to create episode {episode_name}: {e}")

        if len(candidate_ids) > 1:
            consolidated.append(item_info)

        # Update registry record
        reg_data.setdefault("records", {})[fp] = {
            "fingerprint": fp,
            "candidate_ids": candidate_ids,
            "action": action,
            "name": name,
            "episode_name": episode_name,
            "text": content,
            "reference_time": event_dt.isoformat(),
            "event_date": event_date_str,
            "event_date_precision": precision,
            "observed_at": observed_at,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "entities": entities,
            "notes": notes,
            "source": source,
            "reconciled_at": datetime.now(timezone.utc).isoformat(),
        }

    # Save registry
    if not dry_run:
        actual_reg_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(actual_reg_path, "w", encoding="utf-8") as f:
                json.dump(reg_data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save import registry: {e}")

    if format_for_mcp:
        return format_reconcile_results_for_mcp(
            created=created,
            updated=updated,
            consolidated=consolidated,
            discarded=discarded,
            dry_run=dry_run,
            errors=errors,
        )

    return {
        "created": created,
        "updated": updated,
        "consolidated": consolidated,
        "discarded": discarded,
        "errors": errors,
    }


class GraphitiMemoryProvider:
    """Thin class wrapper satisfying server.core.protocols.MemoryProvider.

    Delegates to the module-level functions above rather than
    reimplementing them, so this class carries zero independent logic to
    drift from the free-function API that server.mcp and existing tests
    already depend on. Exists to prove (per the Milestone 1 exit gate) that
    MemoryProvider is a real, implementable protocol and to give a future
    second memory provider a concrete sibling to match.
    """

    async def remember(
        self,
        content: str,
        name: Optional[str] = None,
        source_description: str = "Context Memory Fabric MCP",
        reference_time: Optional[datetime] = None,
        project: Optional[str] = None,
        harness: Optional[str] = None,
    ) -> dict[str, Any]:
        return await remember(
            content=content,
            name=name,
            source_description=source_description,
            reference_time=reference_time,
            project=project,
            harness=harness,
        )

    async def recall(
        self,
        query: str,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        return await recall_mem(query=query, max_results=max_results, format_for_mcp=False)

    async def edit(
        self,
        target_query: str,
        new_reference_time: Optional[str | datetime] = None,
        new_content: Optional[str] = None,
        new_summary: Optional[str] = None,
        new_name: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return await edit_memory(
            target_query=target_query,
            new_reference_time=new_reference_time,
            new_content=new_content,
            new_summary=new_summary,
            new_name=new_name,
            dry_run=dry_run,
            format_for_mcp=False,
        )

    async def reconcile(
        self,
        records: list[dict[str, Any]],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return await reconcile_memories(records=records, dry_run=dry_run, format_for_mcp=False)

    async def close(self) -> None:
        await close_graphiti()


