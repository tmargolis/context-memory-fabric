"""OpenAI as the capture workers' extraction LLM (MS10a).

Promotion uses Graphiti's own OpenAIClient unchanged. This is the other call
path: one synchronous, schema-constrained call per conversation window for
server.policies.reasoning_episode. It uses the Responses API with a strict
JSON schema, as Graphiti does, and the same per-model reasoning effort
Graphiti picks, so both paths behave alike on one model.

The openai SDK retries 408/409/429/5xx with backoff; CMF's Gemini rate
limiter does not apply here.
"""

from __future__ import annotations

import copy
from typing import Any, Optional

from graphiti_core.llm_client.openai_base_client import BaseOpenAIClient

MAX_RETRIES = 4
MAX_OUTPUT_TOKENS = 16_000


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """`schema` in the form OpenAI's strict mode accepts.

    Strict mode requires every object to list all of its properties as
    required and to forbid extra ones. A property that was optional becomes
    nullable instead, so the model can still leave it empty.
    """
    out = copy.deepcopy(schema)

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                required = set(node.get("required", []))
                for name, prop in node["properties"].items():
                    if name not in required:
                        _make_nullable(prop)
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(out)
    return out


def _make_nullable(prop: dict[str, Any]) -> None:
    kind = prop.get("type")
    if isinstance(kind, list):
        if "null" not in kind:
            prop["type"] = [*kind, "null"]
    elif isinstance(kind, str):
        if kind != "null":
            prop["type"] = [kind, "null"]
    elif "anyOf" in prop:
        if {"type": "null"} not in prop["anyOf"]:
            prop["anyOf"] = [*prop["anyOf"], {"type": "null"}]
    else:
        prop["anyOf"] = [dict(prop), {"type": "null"}]
        for key in list(prop):
            if key != "anyOf":
                del prop[key]


def request_kwargs(*, model: str, prompt: str, schema: dict[str, Any], schema_name: str) -> dict[str, Any]:
    """Keyword arguments for `client.responses.create`."""
    kwargs: dict[str, Any] = {
        "model": model,
        "input": [{"role": "user", "content": prompt}],
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": strict_schema(schema),
                "strict": True,
            }
        },
    }
    effort = BaseOpenAIClient._resolve_reasoning_effort(model, "auto")
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    return kwargs


def generate_json(
    *,
    api_key: Optional[str],
    model: str,
    prompt: str,
    schema: dict[str, Any],
    schema_name: str,
) -> str:
    """One schema-constrained call; returns the reply's JSON text."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key, max_retries=MAX_RETRIES)
    try:
        response = client.responses.create(
            **request_kwargs(model=model, prompt=prompt, schema=schema, schema_name=schema_name)
        )
    finally:
        client.close()
    if response.status == "incomplete":
        reason = getattr(response.incomplete_details, "reason", None)
        raise ValueError(f"OpenAI reply was incomplete ({reason}).")
    text = response.output_text
    if not text:
        raise ValueError(f"OpenAI reply had no output text (status={response.status!r}).")
    return text
