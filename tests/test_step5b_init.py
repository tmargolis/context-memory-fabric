import asyncio
import os
import sys

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Ensure OPENAI_API_KEY is not present in the environment
if "OPENAI_API_KEY" in os.environ:
    del os.environ["OPENAI_API_KEY"]

from server.memory import create_graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.llm_client.gemini_client import GeminiClient
from graphiti_core.embedder.gemini import GeminiEmbedder
from graphiti_core.cross_encoder.gemini_reranker_client import GeminiRerankerClient


async def validate_step5b() -> bool:
    print("--- Step 5B Graphiti Initialization & Index Validation ---")

    # 1. Verify OPENAI_API_KEY is not set
    if "OPENAI_API_KEY" in os.environ:
        raise RuntimeError("OPENAI_API_KEY is present in the environment!")
    print("OPENAI_API_KEY in env: False (confirmed absent)")

    # 2. Instantiate Graphiti
    graphiti = create_graphiti()

    # 3. Inspect provider class names
    llm_class = graphiti.llm_client.__class__.__name__
    embedder_class = graphiti.embedder.__class__.__name__
    cross_encoder_class = graphiti.cross_encoder.__class__.__name__
    driver_class = graphiti.driver.__class__.__name__

    print(f"Graph Driver:  {driver_class}")
    print(f"LLM Client:    {llm_class}")
    print(f"Embedder:      {embedder_class}")
    print(f"Cross Encoder: {cross_encoder_class}")

    # Validate exact expected classes
    assert isinstance(graphiti.driver, FalkorDriver), f"Expected FalkorDriver, got {driver_class}"
    assert isinstance(graphiti.llm_client, GeminiClient), f"Expected GeminiClient, got {llm_class}"
    assert isinstance(graphiti.embedder, GeminiEmbedder), f"Expected GeminiEmbedder, got {embedder_class}"
    assert isinstance(graphiti.cross_encoder, GeminiRerankerClient), f"Expected GeminiRerankerClient, got {cross_encoder_class}"

    # 4. Build indices and constraints against FalkorDB
    print("\nCalling graphiti.build_indices_and_constraints()...")
    try:
        await graphiti.build_indices_and_constraints()
        print("build_indices_and_constraints() SUCCESS!")
    except Exception as e:
        print(f"build_indices_and_constraints() FAILED: {e}", file=sys.stderr)
        raise
    finally:
        # 5. Cleanly close driver / connection
        print("Closing Graphiti connection...")
        await graphiti.close()
        print("Graphiti closed cleanly.")

    return True


if __name__ == "__main__":
    success = asyncio.run(validate_step5b())
    if success:
        print("\n=== Phase 1 Step 5B Validation Passed ===")
