"""OpenAI embeddings at the graph's configured width (MS10a).

Graphiti's OpenAIEmbedder requests the model's full width and slices each
vector to `embedding_dim`. text-embedding-3 models can shorten a vector
themselves via `dimensions`, which OpenAI documents as the supported way to
trade width for size, so this sends it. A graph's EMBEDDING_DIM is fixed
once it has data; text-embedding-3-small returns at most 1536.
"""

from __future__ import annotations

from typing import Iterable

from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from openai import AsyncOpenAI

MAX_RETRIES = 4


class DimensionedOpenAIEmbedder(OpenAIEmbedder):
    def __init__(self, config: OpenAIEmbedderConfig) -> None:
        super().__init__(
            config=config,
            client=AsyncOpenAI(api_key=config.api_key, base_url=config.base_url, max_retries=MAX_RETRIES),
        )

    async def create(self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]) -> list[float]:
        result = await self.client.embeddings.create(
            input=input_data, model=self.config.embedding_model, dimensions=self.config.embedding_dim
        )
        return result.data[0].embedding

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        result = await self.client.embeddings.create(
            input=input_data_list, model=self.config.embedding_model, dimensions=self.config.embedding_dim
        )
        return [item.embedding for item in result.data]
