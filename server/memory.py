"""Episodic memory integration with Graphiti and FalkorDB.

Responsible for saving episodic events, decisions, preferences, and state changes,
recalling relevant graph knowledge, and formatting temporal memories with provenance.
"""

import asyncio
from datetime import datetime, timezone
import logging
import os
from typing import Any, Optional

from dotenv import load_dotenv
from graphiti_core import Graphiti
from graphiti_core.cross_encoder.gemini_reranker_client import GeminiRerankerClient
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.embedder.gemini import GeminiEmbedder, GeminiEmbedderConfig
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.gemini_client import GeminiClient
from graphiti_core.nodes import EpisodeType

load_dotenv()
logger = logging.getLogger(__name__)

# Cached Graphiti instance per running event loop
_GLOBAL_GRAPHITI: Optional[Graphiti] = None
_BOUND_LOOP_ID: Optional[int] = None


def create_graphiti() -> Graphiti:
    """Instantiate a Graphiti client configured with FalkorDB and Gemini."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set. Add it to the project-root .env file.")

    falkor_host = os.getenv("FALKORDB_HOST", "localhost")
    falkor_port = int(os.getenv("FALKORDB_PORT", "6379"))
    falkor_password = os.getenv("FALKORDB_PASSWORD") or None

    driver = FalkorDriver(
        host=falkor_host,
        port=falkor_port,
        password=falkor_password,
    )

    llm_client = GeminiClient(
        config=LLMConfig(
            api_key=api_key,
            model="gemini-3.5-flash-lite",
            small_model="gemini-3.5-flash-lite",
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
            model="gemini-3.5-flash-lite",
        )
    )

    return Graphiti(
        graph_driver=driver,
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=cross_encoder,
    )


def get_graphiti() -> Graphiti:
    """Retrieve or initialize the global Graphiti instance for the active event loop."""
    global _GLOBAL_GRAPHITI, _BOUND_LOOP_ID

    try:
        current_loop = asyncio.get_running_loop()
        current_loop_id = id(current_loop)
    except RuntimeError:
        current_loop_id = None

    if _GLOBAL_GRAPHITI is None or _BOUND_LOOP_ID != current_loop_id:
        _GLOBAL_GRAPHITI = create_graphiti()
        _BOUND_LOOP_ID = current_loop_id

    return _GLOBAL_GRAPHITI


async def close_graphiti() -> None:
    """Explicitly close the active Graphiti client."""
    global _GLOBAL_GRAPHITI, _BOUND_LOOP_ID
    if _GLOBAL_GRAPHITI is not None:
        try:
            await _GLOBAL_GRAPHITI.close()
        except Exception as e:
            logger.debug(f"Error closing Graphiti driver: {e}")
        finally:
            _GLOBAL_GRAPHITI = None
            _BOUND_LOOP_ID = None


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
    """
    graphiti = get_graphiti()
    ref_time = reference_time or datetime.now(timezone.utc)
    episode_name = name or f"memory_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    logger.info(f"Ingesting memory episode: '{episode_name}'")

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
            err_msg = str(e).lower()
            if ("429" in err_msg or "resource_exhausted" in err_msg or "quota" in err_msg) and attempt < max_retries - 1:
                backoff = 5.0 * (attempt + 1)
                logger.warning(f"Rate limit encountered in remember(). Retrying in {backoff:.1f}s (attempt {attempt+1}/{max_retries})...")
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
    """
    graphiti = get_graphiti()

    results = []
    for attempt in range(max_retries):
        try:
            results = await graphiti.search(query)
            break
        except Exception as e:
            err_msg = str(e).lower()
            if ("429" in err_msg or "resource_exhausted" in err_msg or "quota" in err_msg) and attempt < max_retries - 1:
                backoff = 5.0 * (attempt + 1)
                logger.warning(f"Rate limit encountered in recall(). Retrying in {backoff:.1f}s (attempt {attempt+1}/{max_retries})...")
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
