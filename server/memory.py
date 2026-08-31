import os

from dotenv import load_dotenv

from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.llm_client.gemini_client import GeminiClient
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.embedder.gemini import (
    GeminiEmbedder,
    GeminiEmbedderConfig,
)
from graphiti_core.cross_encoder.gemini_reranker_client import (
    GeminiRerankerClient,
)


def create_graphiti() -> Graphiti:
    # Loads .env from the project directory/current working directory
    load_dotenv()

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Add it to the project-root .env file."
        )

    falkor_host = os.getenv("FALKORDB_HOST", "localhost")
    falkor_port = int(os.getenv("FALKORDB_PORT", "6379"))

    driver = FalkorDriver(
        host=falkor_host,
        port=falkor_port,
        username=None,
        password=None,
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