"""Claude as an extraction LLM (MS10a), for both of CMF's LLM call paths.

Graphiti 0.29.3's own AnthropicClient can't talk to current Claude models:
it forces a tool call (`tool_choice={"type": "tool"}`) and sends
`temperature`, and claude-opus-5-5 / claude-sonnet-5-5 reject both with a
400. Structured outputs replace the forced tool: `output_config.format`
constrains the reply to the response model's JSON schema, and the first text
block is guaranteed to be JSON that validates against it.

- `StructuredAnthropicClient`: Graphiti's AnthropicClient with only
  `_generate_response` replaced, so its validation and retry loop in
  `generate_response` still apply. Used by promotion (add_episode).
- `generate_json`: one synchronous call for the capture workers'
  reasoning-episode extraction (server.policies.reasoning_episode).

Thinking is left at the model's default (adaptive, always on for Opus 5.5)
and `max_tokens` has a floor, since thinking spends from the same budget.
The SDK retries 408/409/429/5xx (529 overloaded included) with backoff;
CMF's Gemini rate limiter does not apply here.
"""

from __future__ import annotations

import json
import logging
import typing
from typing import Any, Optional

import anthropic
from graphiti_core.llm_client.anthropic_client import AnthropicClient
from graphiti_core.llm_client.config import LLMConfig, ModelSize
from graphiti_core.llm_client.errors import RateLimitError, RefusalError
from graphiti_core.prompts.models import Message
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Thinking and the answer share max_tokens; Graphiti asks for as little as
# 8,192 on some prompts. 16,000 keeps a non-streaming call well inside the
# SDK's HTTP timeout.
MIN_MAX_TOKENS = 16_000
MAX_RETRIES = 4

# Models that accept server-side refusal fallbacks in their "default" form
# (the API picks the fallback by refusal category). Others (Haiku, older
# models) are sent without it.
FALLBACK_MODELS = frozenset({"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"})
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicRefusalError(RuntimeError):
    """The model (and any fallback) declined the request."""


def _type_unions_to_any_of(node: Any) -> Any:
    """`{"type": ["string", "null"]}` as `anyOf` branches.

    CMF's episode schemas mark nullable fields with a type list, which
    anthropic.transform_schema doesn't accept. `anyOf` says the same thing
    in a form it does. A description stays on the outer node.
    """
    if isinstance(node, list):
        return [_type_unions_to_any_of(v) for v in node]
    if not isinstance(node, dict):
        return node
    out = {k: _type_unions_to_any_of(v) for k, v in node.items()}
    kinds = out.get("type")
    if isinstance(kinds, list):
        rest = {k: v for k, v in out.items() if k not in ("type", "description")}
        branches = [{**rest, "type": t} if t != "null" else {"type": "null"} for t in kinds]
        out = {"anyOf": branches}
        if "description" in node:
            out["description"] = node["description"]
    return out


def _output_schema(schema: dict[str, Any] | type[BaseModel]) -> dict[str, Any]:
    if isinstance(schema, dict):
        schema = _type_unions_to_any_of(schema)
    return anthropic.transform_schema(schema)


def request_kwargs(
    *,
    model: str,
    messages: list[dict[str, Any]],
    system: Optional[str] = None,
    schema: Optional[dict[str, Any] | type[BaseModel]] = None,
    max_tokens: Optional[int] = None,
    effort: Optional[str] = None,
) -> dict[str, Any]:
    """Keyword arguments for `client.beta.messages.create`."""
    output_config: dict[str, Any] = {}
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": _output_schema(schema)}
    if effort:
        output_config["effort"] = effort

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max(max_tokens or 0, MIN_MAX_TOKENS),
        "messages": messages,
    }
    if system:
        kwargs["system"] = system
    if output_config:
        kwargs["output_config"] = output_config
    if model in FALLBACK_MODELS:
        kwargs["betas"] = [FALLBACK_BETA]
        kwargs["fallbacks"] = "default"
    return kwargs


def response_text(message: Any) -> str:
    """The reply's JSON text, or a clear error for a refusal or a cut-off reply."""
    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise AnthropicRefusalError(f"Claude declined the request (category: {category}).")
    if message.stop_reason == "max_tokens":
        raise ValueError(f"Claude's reply hit max_tokens ({message.usage.output_tokens} output tokens).")
    for block in message.content:
        if block.type == "text":
            return block.text
    raise ValueError(f"Claude's reply had no text block (stop_reason={message.stop_reason!r}).")


def _usage(message: Any) -> tuple[int, int]:
    usage = getattr(message, "usage", None)
    if usage is None:
        return 0, 0
    return getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0


class StructuredAnthropicClient(AnthropicClient):
    """Graphiti LLM client for current Claude models, using structured outputs."""

    def __init__(self, config: LLMConfig, effort: Optional[str] = None) -> None:
        super().__init__(
            config=config,
            client=anthropic.AsyncAnthropic(api_key=config.api_key, max_retries=MAX_RETRIES),
        )
        self.effort = effort

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int | None = None,
        model_size: ModelSize = ModelSize.medium,
    ) -> tuple[dict[str, typing.Any], int, int]:
        system = messages[0].content if messages and messages[0].role == "system" else None
        turns = messages[1:] if system is not None else messages
        kwargs = request_kwargs(
            model=self.model,
            system=system,
            messages=[{"role": m.role, "content": m.content} for m in turns],
            schema=response_model,
            max_tokens=max_tokens or self.max_tokens,
            effort=self.effort,
        )
        try:
            message = await self.client.beta.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise RateLimitError(f"Anthropic rate limit after retries: {e}") from e
        try:
            text = response_text(message)
        except AnthropicRefusalError as e:
            raise RefusalError(str(e)) from e
        input_tokens, output_tokens = _usage(message)
        if response_model is None:
            return self._extract_json_from_text(text), input_tokens, output_tokens
        return json.loads(text), input_tokens, output_tokens


def generate_json(
    *,
    api_key: Optional[str],
    model: str,
    prompt: str,
    schema: dict[str, Any],
    effort: Optional[str] = None,
) -> str:
    """One schema-constrained call; returns the reply's JSON text."""
    client = anthropic.Anthropic(api_key=api_key, max_retries=MAX_RETRIES)
    try:
        message = client.beta.messages.create(
            **request_kwargs(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                schema=schema,
                effort=effort,
            )
        )
    finally:
        client.close()
    return response_text(message)
