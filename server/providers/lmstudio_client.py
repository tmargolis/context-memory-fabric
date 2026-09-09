"""LM Studio compatibility layer for graphiti_core's OpenAI-generic client.

LM Studio speaks the OpenAI chat-completions API, but two behaviours break
graphiti_core against it. Both are handled here rather than by forking
graphiti's client, so an upgrade of graphiti_core does not silently drop the
workarounds: `OpenAIGenericClient` accepts an injected `client`, and only
ever touches `.chat.completions.create(...)` on it.

1. `response_format: {"type": "json_object"}` is rejected outright --
   `'response_format.type' must be 'json_schema' or 'text'`. graphiti emits
   that shape in its "put the schema in the prompt" mode, which is exactly
   the mode we want (see 2). Rewritten to `{"type": "text"}` on the way out;
   nothing is lost, because graphiti has already appended the schema to the
   final user message by then.

2. LM Studio parses a reasoning model's thinking channel server-side and
   returns it separately as `reasoning_content`. Under `json_schema`
   constrained decoding, GLM-4.7-Flash and Qwen3.5-35B-A3B put their entire
   answer in that channel and leave `content` empty -- and graphiti raises
   EmptyResponseError on an empty `content`. Under plain `text` they behave
   normally (JSON in `content`, prose in `reasoning_content`), which is why
   Mode B is the default. The response-side rescue below exists so Mode A
   stays selectable, and is a no-op in Mode B.

Measured on the DGX Spark, 2026-09-08, identical realistic extraction prompt:

    model                    json_schema                  text + schema in prompt
    zai-org/glm-4.7-flash    answer in reasoning_content   content = valid JSON (17 ent/14 edges)
    qwen/qwen3.5-35b-a3b     answer in reasoning_content   content = valid JSON (14 ent/10 edges)
    google/gemma-4-26b-a4b   control-token corruption      content = valid JSON (12 ent/10 edges)

Gemma's json_schema failure is a serving-stack bug, not a bad quant: a
`<|channel>` control token leaks into a JSON string mid-generation because
the grammar constrains text shape without masking special tokens, and the
distribution then collapses into repeated `6666...` until the token cap.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# LM Studio accepts only these two response_format types.
_SUPPORTED_RESPONSE_FORMATS = ("json_schema", "text")


def _rewrite_response_format(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Map an unsupported response_format onto one LM Studio accepts."""
    response_format = kwargs.get("response_format")
    if not isinstance(response_format, dict):
        return kwargs

    fmt_type = response_format.get("type")
    if fmt_type in _SUPPORTED_RESPONSE_FORMATS:
        return kwargs

    if fmt_type == "json_object":
        kwargs = dict(kwargs)
        kwargs["response_format"] = {"type": "text"}
        return kwargs

    # Anything else is unknown to us AND to LM Studio; dropping the field
    # yields the server default rather than a 400 on a value we cannot map.
    logger.warning(
        "Unrecognised response_format %r for LM Studio; sending without one.", fmt_type
    )
    kwargs = dict(kwargs)
    kwargs.pop("response_format", None)
    return kwargs


def _rescue_reasoning_content(response: Any) -> Any:
    """Promote `reasoning_content` into an empty `content`, when it is JSON.

    Only fires when `content` is empty/whitespace, so it never overwrites a
    real answer. The JSON check is what makes it safe to leave enabled in
    Mode B: there `reasoning_content` holds genuine chain-of-thought prose,
    and feeding that to graphiti's `json.loads` would be strictly worse than
    letting EmptyResponseError fire -- graphiti retries that (it is in
    `is_server_or_retry_error`), and a retry can recover.
    """
    try:
        message = response.choices[0].message
    except (AttributeError, IndexError):
        return response

    if (getattr(message, "content", None) or "").strip():
        return response

    reasoning = getattr(message, "reasoning_content", None)
    if not reasoning or not reasoning.strip():
        return response

    candidate = reasoning.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        json.loads(candidate)
    except (ValueError, TypeError):
        logger.debug(
            "reasoning_content present but not JSON (%d chars); leaving content empty "
            "so graphiti's retry path handles it.",
            len(reasoning),
        )
        return response

    logger.debug(
        "Promoting %d chars of reasoning_content into an empty content field.",
        len(candidate),
    )
    message.content = candidate
    return response


class _Completions:
    def __init__(self, inner: AsyncOpenAI) -> None:
        self._inner = inner

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        response = await self._inner.chat.completions.create(
            *args, **_rewrite_response_format(kwargs)
        )
        return _rescue_reasoning_content(response)


class _Chat:
    def __init__(self, inner: AsyncOpenAI) -> None:
        self.completions = _Completions(inner)


class LMStudioCompatClient:
    """Minimal stand-in for AsyncOpenAI, adapted to LM Studio's quirks.

    Deliberately not a subclass of AsyncOpenAI: graphiti's generic client
    only ever calls `.chat.completions.create(...)`, and duck-typing that one
    surface keeps this immune to changes elsewhere in the OpenAI SDK.
    Attribute access falls through to the real client for anything else.
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        self._inner = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self.chat = _Chat(self._inner)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
