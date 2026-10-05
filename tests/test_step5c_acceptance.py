"""Phase 1 Step 5C acceptance test for Context Memory Fabric.

Validates Graphiti episodic ingestion and retrieval using Gemini + FalkorDB
with a synthetic text episode.
"""

import asyncio
from datetime import datetime, timezone
import os
import sys

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Ensure OPENAI_API_KEY is not present in the environment
if "OPENAI_API_KEY" in os.environ:
    del os.environ["OPENAI_API_KEY"]

from server.memory import create_graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.llm_client.gemini_client import GeminiClient
from graphiti_core.embedder.gemini import GeminiEmbedder
from graphiti_core.cross_encoder.gemini_reranker_client import GeminiRerankerClient
from graphiti_core.nodes import EpisodeType

SYNTHETIC_EPISODE_NAME = "phase1_project_atlas_database_decision"
SYNTHETIC_EPISODE_BODY = (
    "Project Atlas selected PostgreSQL as its primary database on August 31, 2026."
)
SYNTHETIC_SOURCE_DESC = "Context Memory Fabric Phase 1 synthetic acceptance test"
SYNTHETIC_REFERENCE_TIME = datetime(2026, 8, 31, 0, 0, 0, tzinfo=timezone.utc)
SEARCH_QUERY = "Project Atlas database"


def verify_providers(graphiti):
    """Verify that all configured providers are Gemini and FalkorDB, not OpenAI."""
    assert isinstance(graphiti.driver, FalkorDriver), f"Expected FalkorDriver, got {type(graphiti.driver)}"
    assert isinstance(graphiti.llm_client, GeminiClient), f"Expected GeminiClient, got {type(graphiti.llm_client)}"
    assert isinstance(graphiti.embedder, GeminiEmbedder), f"Expected GeminiEmbedder, got {type(graphiti.embedder)}"
    assert isinstance(graphiti.cross_encoder, GeminiRerankerClient), f"Expected GeminiRerankerClient, got {type(graphiti.cross_encoder)}"
    assert "OPENAI_API_KEY" not in os.environ, "OPENAI_API_KEY must not be present"


async def run_session_1_ingest_and_search():
    """Session 1: Ingest synthetic episode and run initial search."""
    print("=== SESSION 1: Ingest & Initial Search ===")
    graphiti = create_graphiti()
    verify_providers(graphiti)
    print(f"Providers verified: FalkorDriver, GeminiClient, GeminiEmbedder, GeminiRerankerClient")

    try:
        print(f"\n1. Ingesting episode '{SYNTHETIC_EPISODE_NAME}'...")
        add_result = await graphiti.add_episode(
            name=SYNTHETIC_EPISODE_NAME,
            episode_body=SYNTHETIC_EPISODE_BODY,
            source_description=SYNTHETIC_SOURCE_DESC,
            reference_time=SYNTHETIC_REFERENCE_TIME,
            source=EpisodeType.text,
        )
        print("add_episode() completed successfully.")
        print(f"  Episode UUID: {add_result.episode.uuid if add_result.episode else 'N/A'}")
        print(f"  Extracted Nodes ({len(add_result.nodes)}): {[n.name for n in add_result.nodes]}")
        print(f"  Extracted Edges ({len(add_result.edges)}): {[e.fact for e in add_result.edges]}")

        print(f"\n2. Running search for '{SEARCH_QUERY}'...")
        results = await graphiti.search(query=SEARCH_QUERY)
        print(f"Search returned {len(results)} edge(s):")
        found_fact = False
        for i, edge in enumerate(results, start=1):
            print(f"  [{i}] fact: {edge.fact}")
            if edge.valid_at:
                print(f"      valid_at: {edge.valid_at}")
            if edge.created_at:
                print(f"      created_at: {edge.created_at}")
            if "PostgreSQL" in edge.fact and "Project Atlas" in edge.fact:
                found_fact = True

        assert found_fact, "Session 1 search failed to find expected fact"
        print("Session 1 search confirmed semantic match.")
        return add_result, results
    finally:
        await graphiti.close()
        print("Session 1 Graphiti client closed cleanly.\n")


async def run_session_2_persistence_test():
    """Session 2: Fresh client instance that ONLY searches (persistence test)."""
    print("=== SESSION 2: Independent Persistence Test ===")
    graphiti = create_graphiti()
    verify_providers(graphiti)
    print(f"Providers verified: FalkorDriver, GeminiClient, GeminiEmbedder, GeminiRerankerClient")

    try:
        print(f"Querying fresh Graphiti session for: '{SEARCH_QUERY}' without adding episode...")
        results = await graphiti.search(query=SEARCH_QUERY)
        print(f"Search returned {len(results)} edge(s):")
        found_fact = False
        for i, edge in enumerate(results, start=1):
            print(f"  [{i}] fact: {edge.fact}")
            if edge.valid_at:
                print(f"      valid_at: {edge.valid_at}")
            if edge.created_at:
                print(f"      created_at: {edge.created_at}")
            if "PostgreSQL" in edge.fact and "Project Atlas" in edge.fact:
                found_fact = True

        assert found_fact, "Session 2 persistence test failed to find expected fact"
        print("Session 2 persistence test SUCCESS: Fact recalled across sessions.")
        return results
    finally:
        await graphiti.close()
        print("Session 2 Graphiti client closed cleanly.\n")


async def main():
    print("------------------------------------------------------------")
    print("Starting Context Memory Fabric Phase 1 Step 5C Acceptance Test")
    print("------------------------------------------------------------\n")
    await run_session_1_ingest_and_search()
    await run_session_2_persistence_test()
    print("============================================================")
    print("Phase 1 Step 5C Acceptance Test PASSED completely!")
    print("============================================================")


if __name__ == "__main__":
    asyncio.run(main())
