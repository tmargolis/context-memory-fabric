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
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Literal, Optional, Union, overload

from dotenv import load_dotenv
from graphiti_core import Graphiti
from graphiti_core.cross_encoder.gemini_reranker_client import GeminiRerankerClient
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.embedder.gemini import GeminiEmbedder, GeminiEmbedderConfig
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.gemini_client import GeminiClient
from graphiti_core.nodes import EpisodeType

from server.core.rate_limiter import GeminiQuotaExhaustedError, get_default_rate_limiter

load_dotenv()
logger = logging.getLogger(__name__)

# Cached Graphiti instances per (event_loop_id, graph_name, model)
_GRAPHITI_INSTANCES: dict[tuple[Optional[int], str], Graphiti] = {}

# Transient-error classification for remember()/recall()'s retry loops.
# "429"/"resource_exhausted"/"quota"/"rate limit" catch real quota errors
# (which get_graphiti_for_operation()'s rate-limit reservation should mostly
# prevent from happening at all, but a call outside this process's own
# ledger — e.g. concurrent AI Studio usage — can still trigger one).
# "503"/"unavailable"/"high demand" catch transient server-side capacity
# errors, unrelated to quota, observed independently on more than one
# Gemini model during this milestone's own testing — see the MS4a section
# of IMPLEMENTATION-PLAN.md for the specifics. Neither category implies a
# specific model is permanently broken; both are worth a backoff retry.
_TRANSIENT_ERROR_MARKERS = {
    "quota": ("429", "resource_exhausted", "quota", "rate limit"),
    "unavailable": ("503", "unavailable", "high demand", "overloaded"),
}


def _classify_transient_error(exc: Exception) -> Optional[str]:
    err_msg = str(exc).lower()
    for label, markers in _TRANSIENT_ERROR_MARKERS.items():
        if any(marker in err_msg for marker in markers):
            return label
    return None


def _is_transient_gemini_error(exc: Exception) -> bool:
    return _classify_transient_error(exc) is not None


class MissingGraphConfigurationError(RuntimeError):
    """Raised when no FalkorDB target graph can be resolved.

    FALKORDB_DATABASE previously defaulted silently to "default_db" when unset.
    That caused production reads/writes to diverge from the graph an operator
    believed was configured (see docs/adr/0003-graph-and-state-topology.md).
    Failing loudly here is deliberate: a misconfigured deployment must not
    silently read or write the wrong episodic graph.
    """


def resolve_target_database(graph_name: Optional[str] = None) -> str:
    """Resolve the FalkorDB graph name to use, with no implicit default.

    Args:
        graph_name: Explicit override (e.g. from an MCP tool argument). Takes
            precedence over environment configuration when non-empty.

    Returns:
        The resolved graph name.

    Raises:
        MissingGraphConfigurationError: if neither `graph_name` nor the
            `FALKORDB_DATABASE` environment variable is set.
    """
    if graph_name and graph_name.strip():
        return graph_name.strip()

    env_value = os.getenv("FALKORDB_DATABASE")
    if env_value and env_value.strip():
        return env_value.strip()

    raise MissingGraphConfigurationError(
        "FALKORDB_DATABASE is not set and no graph_name was provided. "
        "Set FALKORDB_DATABASE in the project-root .env file (e.g. "
        "FALKORDB_DATABASE=memory-fabric) to select the target FalkorDB graph. "
        "There is no default graph name."
    )


def create_graphiti(graph_name: Optional[str] = None, model: Optional[str] = None) -> Graphiti:
    """Instantiate a Graphiti client configured with FalkorDB and Gemini.

    `model` selects the LLM used for entity/fact extraction and reranking
    (the embedder always uses gemini-embedding-001, which has ample
    free-tier headroom and isn't part of the rate-limited fallback chain).
    Defaults to the first model in the configured chain when omitted — used
    for construction paths (edit_memory, reconcile_memories' Cypher access)
    that need a Graphiti instance but never actually invoke the LLM client,
    so no rate-limit reservation applies there. Callers that DO invoke the
    LLM (remember/recall) must go through get_graphiti_for_operation()
    instead, which reserves a model from the rate limiter first.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set. Add it to the project-root .env file.")

    falkor_host = os.getenv("FALKORDB_HOST", "localhost")
    falkor_port = int(os.getenv("FALKORDB_PORT", "6379"))
    falkor_password = os.getenv("FALKORDB_PASSWORD") or None
    target_database = resolve_target_database(graph_name)
    resolved_model = model or get_default_rate_limiter().chain[0]

    driver = FalkorDriver(
        host=falkor_host,
        port=falkor_port,
        password=falkor_password,
        database=target_database,
    )

    llm_client = GeminiClient(
        config=LLMConfig(
            api_key=api_key,
            model=resolved_model,
            small_model=resolved_model,
        )
    )

    embedder = GeminiEmbedder(
        config=GeminiEmbedderConfig(
            api_key=api_key,
            embedding_model="gemini-embedding-001",
        )
    )

    cross_encoder = GeminiRerankerClient(
        config=LLMConfig(
            api_key=api_key,
            model=resolved_model,
        )
    )

    return Graphiti(
        graph_driver=driver,
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=cross_encoder,
    )


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
    Gemini (remember() via add_episode, recall() via search()) — it commits
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


async def remember(
    content: str,
    name: Optional[str] = None,
    source_description: str = "Context Memory Fabric MCP",
    reference_time: Optional[datetime] = None,
    max_retries: int = 3,
) -> dict[str, Any]:
    """Ingest a new episode into episodic memory (Graphiti + FalkorDB).

    Args:
        content: The text content to store as an episodic memory.
        name: An identifier name for the episode (auto-generated if omitted).
        source_description: Description of the memory origin.
        reference_time: Time when the event occurred (defaults to now UTC).
        max_retries: Number of retries on transient rate limits (429).

    Raises:
        GeminiQuotaExhaustedError: if every model in the configured free-tier
            chain lacks headroom right now. No Gemini call is made in this
            case — safe to retry later (e.g. next RPM window or day
            boundary) without having burned any quota on a failed attempt.
    """
    graphiti, chosen_model = get_graphiti_for_operation()
    ref_time = reference_time or datetime.now(timezone.utc)
    episode_name = name or f"memory_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    logger.info(f"Ingesting memory episode: '{episode_name}' (model: {chosen_model})")

    for attempt in range(max_retries):
        try:
            await graphiti.add_episode(
                name=episode_name,
                episode_body=content,
                source_description=source_description,
                reference_time=ref_time,
                source=EpisodeType.text,
            )
            break
        except Exception as e:
            if _is_transient_gemini_error(e) and attempt < max_retries - 1:
                backoff = 25.0 * (attempt + 1)
                logger.warning(
                    f"Transient Gemini error in remember() ({_classify_transient_error(e)}). "
                    f"Retrying in {backoff:.1f}s (attempt {attempt+1}/{max_retries})..."
                )
                await asyncio.sleep(backoff)
            else:
                raise

    return {
        "status": "success",
        "name": episode_name,
        "reference_time": ref_time.isoformat(),
        "source_description": source_description,
        "message": f"Successfully remembered episode '{episode_name}' in episodic memory.",
    }


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

        status_tag = "ACTIVE" if not invalid_at else f"SUPERSEDED (invalidated at {invalid_at})"
        lines.append(f"#### {idx}. {fact_text}")
        lines.append(f"- **Status:** `{status_tag}`")
        lines.append(f"- **Valid From:** `{valid_at}`")
        if invalid_at:
            lines.append(f"- **Superseded At:** `{invalid_at}`")
        if episodes:
            lines.append(f"- **Source Episodes:** `{', '.join(str(e) for e in episodes)}`")
        lines.append("")

    return "\n".join(lines).strip()


@overload
async def recall(
    query: str,
    max_results: int = 10,
    format_for_mcp: Literal[True] = True,
    max_retries: int = 3,
) -> str: ...


@overload
async def recall(
    query: str,
    max_results: int = 10,
    format_for_mcp: Literal[False] = ...,
    max_retries: int = 3,
) -> list[dict[str, Any]]: ...


@overload
async def recall(
    query: str,
    max_results: int = 10,
    format_for_mcp: bool = ...,
    max_retries: int = 3,
) -> str | list[dict[str, Any]]: ...


async def recall(
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
    graphiti, _chosen_model = get_graphiti_for_operation()

    results = []
    for attempt in range(max_retries):
        try:
            results = await graphiti.search(query)
            break
        except Exception as e:
            if _is_transient_gemini_error(e) and attempt < max_retries - 1:
                backoff = 5.0 * (attempt + 1)
                logger.warning(
                    f"Transient Gemini error in recall() ({_classify_transient_error(e)}). "
                    f"Retrying in {backoff:.1f}s (attempt {attempt+1}/{max_retries})..."
                )
                await asyncio.sleep(backoff)
            else:
                raise

    facts: list[dict[str, Any]] = []
    for edge in results[:max_results]:
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

        facts.append(fact_data)

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

    return "\n".join(lines).strip()


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
) -> str | dict[str, Any]:
    """Edit, correct, or re-date existing episodic memory episodes, entities, and edges in FalkorDB.

    Args:
        target_query: Search string, entity name, episode name, or UUID identifying the memory to edit.
        new_reference_time: New date/timestamp for the episode (e.g. '2025-01-13' or '2025-01-13T00:00:00Z').
        new_content: Optional updated body content for the episode.
        new_summary: Optional updated summary for matched entity node(s).
        new_name: Optional updated identifier name for the episode or entity.
        dry_run: If True, previews changes without writing to FalkorDB or updating the import registry.
        format_for_mcp: If True, returns formatted Markdown for MCP responses.
        registry_path: Path to import registry JSON (defaults to project-root imports/state/import_registry.json).
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

    # 3. Find connected Edges
    edges_map: dict[str, dict[str, Any]] = {}
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

    # 4. Plan and track modifications
    modified_episodes: list[dict[str, Any]] = []
    modified_entities: list[dict[str, Any]] = []
    modified_edges: list[dict[str, Any]] = []
    registry_updates: list[dict[str, Any]] = []

    # Extract old date string candidates from matched episodes if re-dating
    old_date_strs: set[str] = set()
    old_compact_strs: set[str] = set()

    for ep in episodes_map.values():
        old_val = ep.get("valid_at")
        if old_val:
            d_match = re.search(r"(\d{4}-\d{2}-\d{2})", str(old_val))
            if d_match:
                old_date_strs.add(d_match.group(1))
                old_compact_strs.add(d_match.group(1).replace("-", ""))

    for ep in episodes_map.values():
        old_content = ep.get("content") or ""
        for m in re.finditer(r"\b(\d{4}-\d{2}-\d{2})\b", old_content):
            old_date_strs.add(m.group(1))
            old_compact_strs.add(m.group(1).replace("-", ""))

    # Process Episode modifications
    for ep_uuid, ep in episodes_map.items():
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
            }
            modified_episodes.append(ep_change)

            if not dry_run:
                await driver.execute_query(
                    "MATCH (e:Episodic {uuid: $uuid}) SET e.valid_at = $valid_at, e.content = $content, e.name = $name",
                    uuid=ep_uuid,
                    valid_at=target_valid_at,
                    content=target_content,
                    name=target_name,
                )

    # Process Entity modifications
    for ent_uuid, ent in entities_map.items():
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

    # Process Edge modifications
    for edge_uuid, edge in edges_map.items():
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
    actual_reg_path = Path(registry_path) if registry_path else Path(__file__).resolve().parent.parent / "imports" / "state" / "import_registry.json"
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

    result_data = {
        "target_query": clean_query,
        "dry_run": dry_run,
        "matched_episodes": modified_episodes,
        "matched_entities": modified_entities,
        "matched_edges": modified_edges,
        "registry_updates": registry_updates,
    }

    if format_for_mcp:
        return format_edit_memory_results_for_mcp(
            target_query=clean_query,
            dry_run=dry_run,
            matched_episodes=modified_episodes,
            matched_entities=modified_entities,
            matched_edges=modified_edges,
            registry_updates=registry_updates,
        )
    return result_data


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
    actual_reg_path = Path(registry_path) if registry_path else Path(__file__).resolve().parent.parent / "imports" / "state" / "import_registry.json"

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
    ) -> dict[str, Any]:
        return await remember(
            content=content,
            name=name,
            source_description=source_description,
            reference_time=reference_time,
        )

    async def recall(
        self,
        query: str,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        return await recall(query=query, max_results=max_results, format_for_mcp=False)

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


