"""Quota metering for the Gemini embedder.

`server/core/rate_limiter.py` exists to "guarantee CMF never places a Gemini
call that would exceed the free tier's per-model RPM/RPD ceilings". It did not
do that for embeddings, and embeddings are the ceiling CMF actually hits.

A measured `add_episode` issues **3** generation calls and **~20** embedding
calls -- graphiti embeds every entity name and every edge fact individually.
Against the free tier's 1,000 embeddings/day that caps throughput at roughly
50 episodes/day, well under the ~160/day the 500 RPD generation limit
suggests. `gemini-embedding-001` had a budget defined in KNOWN_MODEL_BUDGETS
but was never in DEFAULT_MODEL_CHAIN, so `reserve()` never debited it and the
ledger never tracked it -- it reported ample headroom right up to a live
429 RESOURCE_EXHAUSTED.

Metering has to happen here rather than at a CMF call site because there is
no CMF call site: graphiti calls `embedder.create()` / `create_batch()` from
inside `add_episode` and `search`. Wrapping the embedder is the only place
that sees every request.

Not needed on the local path -- nomic-embed-text has no quota -- so
`maybe_meter` returns the embedder untouched there rather than adding a layer
that only ever says yes.
"""

from __future__ import annotations

from collections.abc import Iterable
import logging

from graphiti_core.embedder.client import EmbedderClient

from server.core.rate_limiter import GeminiRateLimiter, get_default_rate_limiter

logger = logging.getLogger(__name__)

GEMINI_EMBEDDING_MODEL = "gemini-embedding-001"


class MeteredEmbedder(EmbedderClient):
    """Reserves free-tier headroom before delegating to the real embedder.

    Reserves *before* the call, so a caller that gets GeminiQuotaExhaustedError
    has made zero API requests and can defer cleanly -- the same contract
    `get_graphiti_for_operation()` already provides for generation.
    """

    def __init__(
        self,
        inner: EmbedderClient,
        model: str = GEMINI_EMBEDDING_MODEL,
        rate_limiter: GeminiRateLimiter | None = None,
    ) -> None:
        self._inner = inner
        self._model = model
        self._rate_limiter = rate_limiter or get_default_rate_limiter()

    @property
    def config(self):  # graphiti reads embedder.config in places
        return self._inner.config

    async def create(
        self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]
    ) -> list[float]:
        self._rate_limiter.reserve_model(self._model, calls=1)
        return await self._inner.create(input_data)

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        """One reservation per input, not per batch.

        GeminiEmbedder forces `batch_size = 1` for gemini-embedding-001 (the
        API caps instances per request), so a "batch" of N is N separate HTTP
        requests and N debits against the daily quota. Counting the batch as
        one call is exactly the undercount that let this quota run out
        unnoticed.
        """
        count = max(1, len(input_data_list))
        self._rate_limiter.reserve_model(self._model, calls=count)
        return await self._inner.create_batch(input_data_list)


def maybe_meter(embedder: EmbedderClient, embed_is_local: bool) -> EmbedderClient:
    """Wrap the embedder on the Gemini path; pass it through on the local one."""
    if embed_is_local:
        return embedder
    return MeteredEmbedder(embedder)
