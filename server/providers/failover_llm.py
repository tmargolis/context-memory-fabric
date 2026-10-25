"""Ranked LLM providers (docs/setup-simplification-plan.md, item 1.8).

CMF_LLM_PROVIDERS=anthropic,openai,gemini: each extraction call goes to the
first provider; when that one refuses for a reason another provider would
not share (rate limit, quota, billing, overload, a rejected key), the same
call is retried on the next, and the log says which provider served it.

Failures that are setup mistakes, such as an unreachable local model server
or a model name the provider doesn't know, are raised as before rather than
quietly moved to the next provider: a Spark that is off must not silently
turn into paid API calls. Embeddings never fail over (vectors from two
embedders can't share a graph).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from graphiti_core.llm_client.client import LLMClient

logger = logging.getLogger(__name__)


def _failover_kinds() -> frozenset[str]:
    from server.core import tool_errors as te

    return frozenset({te.PROVIDER_AUTH, te.PROVIDER_BILLING, te.PROVIDER_QUOTA, te.PROVIDER_UNAVAILABLE, te.MISSING_KEY})


def should_fail_over(exc: BaseException) -> Optional[str]:
    """The error kind when `exc` is one another provider could get past, else None."""
    from server.core import tool_errors as te

    # SDK errors carry the HTTP status; it is surer than the message text.
    status = getattr(exc, "status_code", None)
    if status == 429:
        return te.PROVIDER_QUOTA
    if status in (503, 529):
        return te.PROVIDER_UNAVAILABLE
    try:
        classified = te.classify(exc)
    except Exception:  # noqa: BLE001 - never let classification mask the real error
        return None
    if classified and classified.kind in _failover_kinds():
        return classified.kind
    return None


class FailoverLLMClient(LLMClient):
    """Delegates to [(provider, client), ...] in order; see the module docstring.

    Graphiti reads .config, .model, .small_model, .token_tracker and calls
    set_tracer() and generate_response(); the first client answers for the
    attributes, every client gets the tracer.
    """

    def __init__(self, clients: list[tuple[str, LLMClient]]):
        if not clients:
            raise ValueError("FailoverLLMClient needs at least one provider")
        self.clients = clients
        first = clients[0][1]
        # Not super().__init__: the attributes below are the first client's own.
        self.config = first.config
        self.model = first.model
        self.small_model = first.small_model
        self.temperature = first.temperature
        self.max_tokens = first.max_tokens
        self.cache_enabled = False
        self.cache_dir = None
        self.token_tracker = first.token_tracker
        self.tracer = first.tracer
        self.last_served_by: Optional[str] = None

    @property
    def providers(self) -> list[str]:
        return [p for p, _ in self.clients]

    def set_tracer(self, tracer: Any) -> None:
        self.tracer = tracer
        for _, client in self.clients:
            client.set_tracer(tracer)

    async def generate_response(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        skipped: list[str] = []
        for i, (provider, client) in enumerate(self.clients):
            try:
                result = await client.generate_response(*args, **kwargs)
            except Exception as e:
                kind = should_fail_over(e)
                if kind is None or i == len(self.clients) - 1:
                    raise
                logger.warning(f"LLM provider {provider} refused the call ({kind}); trying {self.clients[i + 1][0]}.")
                skipped.append(f"{provider} ({kind})")
                continue
            self.last_served_by = provider
            if skipped:
                logger.info(f"LLM call served by {provider} after {', '.join(skipped)}.")
            return result
        raise RuntimeError("unreachable")  # pragma: no cover - the loop returns or raises

    async def _generate_response(self, *args: Any, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        # Abstract in LLMClient; generate_response above never calls it.
        return await self.clients[0][1]._generate_response(*args, **kwargs)
