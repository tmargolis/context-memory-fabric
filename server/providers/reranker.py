"""Cross-encoder selection for the local-inference path.

`GeminiRerankerClient` cannot follow CMF onto the Spark, and the obvious
substitute does not work either: graphiti's `OpenAIRerankerClient` scores
passages with `logit_bias={'6432': 1, '7983': 1}` -- hardcoded OpenAI BPE ids
for " True"/" False". Those ids mean unrelated tokens under GLM's or Qwen's
tokenizer, so pointing it at LM Studio produces confident nonsense rather
than an error. Do not use it here.

That leaves two real options, selected by CMF_RERANKER:

- `bge` -- graphiti's BGERerankerClient, a genuine local cross-encoder
  (BAAI/bge-reranker-v2-m3). Needs `sentence-transformers`, which pulls
  torch (~2 GB), and downloads ~2.2 GB of weights on first construction.
  This is the quality option and the eventual default.
- `passthrough` -- keeps whatever order graphiti's own RRF search already
  produced. Adds no signal, but adds no dependency either, so a test run or
  a first bring-up never blocks on a multi-gigabyte install.

Passthrough is the current default deliberately: reranking is a refinement
over an already-ranked candidate set, and making a torch install a
precondition for first light gets the order of work wrong.
"""

from __future__ import annotations

import logging

from graphiti_core.cross_encoder.client import CrossEncoderClient

logger = logging.getLogger(__name__)

PASSTHROUGH = "passthrough"
BGE = "bge"
VALID_RERANKERS = (PASSTHROUGH, BGE)


class PassthroughReranker(CrossEncoderClient):
    """Preserves the caller's ordering, with monotonically decreasing scores.

    Scores are positional, not semantic. They exist because callers expect a
    (passage, score) pair and may sort or threshold on it; they carry no
    information beyond "whatever ranked these already".

    Safe today only because nothing invokes it: CMF's single retrieval call
    site is `graphiti.search(query)`, which resolves to the
    EDGE_HYBRID_SEARCH_RRF recipe and reranks by reciprocal rank fusion
    without touching the cross-encoder at all. Verified by instrumenting
    `rank()` across a real search: zero invocations.

    That makes it a trap for later work. If a search config is ever switched
    to a `cross_encoder` reranker -- MS7's context-assembly work is the
    likely occasion -- this class would silently return the input order with
    plausible-looking scores, and the only symptom would be retrieval quality
    that never improved. The warning below exists so that shows up in a log
    instead of in a confusing evaluation result.
    """

    def __init__(self) -> None:
        self._warned = False

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        if not passages:
            return []
        if not self._warned:
            self._warned = True
            logger.warning(
                "PassthroughReranker.rank() was called: a search config is requesting real "
                "cross-encoder reranking, and this returns the input order unchanged. Set "
                "CMF_RERANKER=bge for actual reranking, or accept unranked results knowingly."
            )
        n = len(passages)
        return [(p, 1.0 - (i / n)) for i, p in enumerate(passages)]


def make_cross_encoder(kind: str) -> CrossEncoderClient:
    """Build the configured cross-encoder, failing clearly if unavailable."""
    if kind == PASSTHROUGH:
        return PassthroughReranker()

    if kind == BGE:
        try:
            from graphiti_core.cross_encoder.bge_reranker_client import BGERerankerClient
        except ImportError as exc:
            raise RuntimeError(
                "CMF_RERANKER=bge needs sentence-transformers, which is not installed. "
                "Install it with `uv add sentence-transformers` (pulls torch, ~2 GB, and "
                "downloads ~2.2 GB of BAAI/bge-reranker-v2-m3 weights on first use), or "
                "set CMF_RERANKER=passthrough."
            ) from exc
        logger.info("Constructing BGERerankerClient — first run downloads model weights.")
        return BGERerankerClient()

    raise ValueError(
        f"CMF_RERANKER={kind!r} is not recognised. Valid values are {', '.join(VALID_RERANKERS)}."
    )
