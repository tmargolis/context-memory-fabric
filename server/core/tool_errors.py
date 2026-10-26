"""Turn foreseeable failures into one plain sentence that says what to do.

The MCP SDK hides any unexpected exception from the client: the model sees
only "Error executing tool <name>", and the traceback goes to a server log the
user may not know exists (docs/setup-simplification-plan.md, P6). The failures
a new install actually hits -- FalkorDB not reachable, a missing or rejected
API key, an unfunded account, an embedding width that doesn't match the graph
-- are foreseeable, so `classify()` recognises them and the tools raise
`ToolError` with its sentence instead.

`classify()` returns None for anything it doesn't recognise; callers re-raise
those unchanged, so a real bug still logs its traceback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from server.core.rate_limiter import GeminiQuotaExhaustedError, classify_transient_error, is_billing_error

# Kinds, also stored with failed background writes (server/memory_writes.py).
FALKORDB_UNREACHABLE = "falkordb_unreachable"
FALKORDB_AUTH = "falkordb_auth"
EMPTY_GRAPH = "empty_graph"
EMBEDDING_MISMATCH = "embedding_mismatch"
MISSING_KEY = "missing_key"
PROVIDER_AUTH = "provider_auth"
PROVIDER_BILLING = "provider_billing"
PROVIDER_QUOTA = "provider_quota"
PROVIDER_UNAVAILABLE = "provider_unavailable"
PROVIDER_UNREACHABLE = "provider_unreachable"
MODEL_NOT_FOUND = "model_not_found"
PROMPT_TOO_LONG = "prompt_too_long"

NO_MEMORIES_YET = (
    "No memories yet: the memory graph is empty or hasn't been created. "
    "It appears after the first memory is saved (remember or capture_session)."
)

_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}

# FalkorDB: a read against a graph key that doesn't exist yet, or a search
# that runs before Graphiti's background index build has created its indexes.
_EMPTY_GRAPH_MARKERS = ("empty key", "unknown index", "no such index")

_AUTH_MARKERS = (
    "api key not valid",
    "api_key_invalid",
    "invalid x-api-key",
    "incorrect api key",
    "invalid api key",
    "authentication_error",
    "permission_denied",
    "unauthenticated",
)

_MODEL_NOT_FOUND_MARKERS = ("model_not_found", "not_found_error", "is not found for api version", "no such model")

# A prompt longer than the model's context (B18/B20). LM Studio / llama.cpp say
# "n_keep: 12119 >= n_ctx: 8192"; OpenAI-compatible servers and hosted APIs say
# "context length" / "maximum context" / "prompt is too long".
_PROMPT_TOO_LONG_MARKERS = (
    "n_ctx",
    "context length",
    "context_length_exceeded",
    "maximum context",
    "prompt is too long",
    "prompt too long",
)


class PromptTooLongError(RuntimeError):
    """Raised by CMF itself before sending a prompt that can't fit the model's context."""


def is_prompt_too_long(exc: BaseException) -> bool:
    for e in _chain(exc):
        if isinstance(e, PromptTooLongError):
            return True
        text = str(e).lower()
        if any(m in text for m in _PROMPT_TOO_LONG_MARKERS):
            return True
    return False


def prompt_too_long_message(config=None) -> str:
    from server.providers.context_limit import context_tokens

    if config is None:
        from server.core.config import load_config

        config = load_config()
    limit = context_tokens(config)
    if config.llm_is_local:
        return (
            f"The memory was too long for the local model's context (loaded at {limit:,} tokens). "
            "Reload the model with a larger context (32,768 or more) in LM Studio, then retry it "
            "(CMF management → Status, or `cmf memory retry`)."
        )
    return (
        f"The memory was too long for the {config.llm_provider} model's context. "
        "Retry it from CMF management → Status, or `cmf memory retry`."
    )


@dataclass(frozen=True)
class ClassifiedError:
    kind: str
    message: str


def _chain(exc: BaseException) -> list[BaseException]:
    """exc and every exception it was raised from or during, outermost first."""
    seen: list[BaseException] = []
    current: Optional[BaseException] = exc
    while current is not None and current not in seen and len(seen) < 10:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def _sdk(exc: BaseException) -> Optional[str]:
    module = type(exc).__module__ or ""
    if module.startswith("anthropic"):
        return "anthropic"
    if module.startswith("openai"):
        return "openai"
    if module.startswith("google"):
        return "gemini"
    return None


def _status(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _request_url(exc: BaseException) -> str:
    try:
        return str(exc.request.url)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - not every exception carries a request
        return ""


def _provider_for(exc: BaseException, config) -> tuple[str, str]:
    """(provider, where): which configured provider an SDK exception came from."""
    sdk = _sdk(exc)
    configured = {config.llm_provider, config.embed_provider}
    if sdk == "openai":
        url = _request_url(exc)
        for base in (config.local_llm_base_url, config.local_embed_base_url):
            if url and base and url.startswith(base.rstrip("/")):
                return "local", base
        if "local" in configured and "openai" not in configured:
            return "local", config.local_llm_base_url
    if sdk:
        return sdk, ""
    # No SDK type to go on: name the extraction provider.
    return config.llm_provider, ""


def _falkordb_where() -> tuple[str, str]:
    from server.core.falkordb_conn import connection_params
    from server.providers.memory_graphiti import resolve_target_database

    params = connection_params()
    return f"{params['host']}:{params['port']}", resolve_target_database()


def _falkordb_unreachable() -> ClassifiedError:
    where, graph = _falkordb_where()
    return ClassifiedError(
        FALKORDB_UNREACHABLE,
        f"CMF can't reach its database (FalkorDB at {where}, graph '{graph}'). "
        "Check that Docker is running and the FalkorDB container is up (`docker ps`). "
        "In the CMF stack FalkorDB runs as the cmf-falkordb container; `cmf status` checks it "
        "and `cmf doctor --fix` restarts what it can (docs/SETUP.md, 'Pick ONE way to run CMF').",
    )


def classify(exc: BaseException, config=None) -> Optional[ClassifiedError]:
    """A plain-language explanation for a foreseeable failure, or None."""
    if config is None:
        from server.core.config import load_config

        config = load_config()

    for e in _chain(exc):
        text = str(e).lower()
        name = type(e).__name__
        module = type(e).__module__ or ""

        if isinstance(e, PromptTooLongError) or any(m in text for m in _PROMPT_TOO_LONG_MARKERS):
            return ClassifiedError(PROMPT_TOO_LONG, prompt_too_long_message(config))

        if isinstance(e, GeminiQuotaExhaustedError):
            return ClassifiedError(
                PROVIDER_QUOTA,
                "Gemini's free-tier limit for today (or this minute) is used up, so CMF made no call. "
                "Try again later, or add another provider's key and set CMF_LLM_PROVIDER to it.",
            )

        # FalkorDB (redis protocol). AuthenticationError subclasses ConnectionError.
        if module.startswith("redis"):
            if name in ("AuthenticationError", "AuthenticationWrongNumberOfArgsError") or "wrongpass" in text:
                where, _ = _falkordb_where()
                return ClassifiedError(
                    FALKORDB_AUTH,
                    f"FalkorDB at {where} rejected CMF's login. Check FALKORDB_USER and FALKORDB_PASSWORD in .env.",
                )
            if name in ("ConnectionError", "TimeoutError", "BusyLoadingError"):
                return _falkordb_unreachable()
            if any(m in text for m in _EMPTY_GRAPH_MARKERS):
                return ClassifiedError(EMPTY_GRAPH, NO_MEMORIES_YET)
            if "dimension" in text and ("vector" in text or "vecf32" in text):
                return _embedding_mismatch(config)
        if isinstance(e, ConnectionRefusedError) and not _sdk(e):
            return _falkordb_unreachable()

        if "embedding_dim" in text and "dimension" in text:
            return _embedding_mismatch(config)

        if "is not set, but" in text or ("_api_key is not set" in text):
            return ClassifiedError(
                MISSING_KEY,
                f"{e} Leave unused keys empty and set CMF_LLM_PROVIDER / CMF_EMBED_PROVIDER to providers "
                "you have keys for. Change them in CMF management (Features → LLM for extraction, Embeddings), which "
                "restarts the server for you.",
            )

        sdk = _sdk(e)
        if sdk is None and not any(m in text for m in _AUTH_MARKERS + _MODEL_NOT_FOUND_MARKERS):
            continue
        provider, where = _provider_for(e, config)
        key_env = _KEY_ENV.get(provider)
        status = _status(e)

        if name in ("APIConnectionError", "APITimeoutError", "ConnectError"):
            if provider == "local":
                return ClassifiedError(
                    PROVIDER_UNREACHABLE,
                    f"CMF can't reach the local model server at {where or 'CMF_LOCAL_BASE_URL'}. Start it "
                    "(LM Studio, Ollama, vLLM) or fix CMF_LOCAL_LLM_BASE_URL / CMF_LOCAL_EMBED_BASE_URL. "
                    "From inside Docker, the host is host.docker.internal, not 127.0.0.1.",
                )
            return ClassifiedError(
                PROVIDER_UNREACHABLE,
                f"CMF can't reach the {provider} API (network error). Check the computer's internet connection.",
            )

        if is_billing_error(e):
            return ClassifiedError(
                PROVIDER_BILLING,
                f"The {provider} account has no credit left, so the call was refused. Add credit in the "
                f"{provider} console (API billing is separate from a Claude or ChatGPT subscription), "
                "or switch CMF_LLM_PROVIDER / CMF_EMBED_PROVIDER to another provider.",
            )

        if status in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError") or any(
            m in text for m in _AUTH_MARKERS
        ):
            fix = f"Check {key_env} in .env" if key_env else "Check the provider's API key in .env"
            return ClassifiedError(
                PROVIDER_AUTH,
                f"{provider} rejected CMF's API key. {fix}: it must be a real API key with no "
                "placeholder text. Change it in CMF management (Features → Provider keys), which restarts the server for you.",
            )

        if (status == 404 or name == "NotFoundError" or any(m in text for m in _MODEL_NOT_FOUND_MARKERS)) and (
            "model" in text
        ):
            return ClassifiedError(
                MODEL_NOT_FOUND,
                f"{provider} doesn't recognise the configured model. Check the model setting for "
                f"{provider} in .env (CMF_ANTHROPIC_MODEL, CMF_OPENAI_MODEL, CMF_OPENAI_EMBED_MODEL, "
                "CMF_LOCAL_LLM_MODEL or CMF_LOCAL_EMBED_MODEL), or unset it to use the default.",
            )

        transient = classify_transient_error(e) if isinstance(e, Exception) else None
        if transient == "quota":
            return ClassifiedError(
                PROVIDER_QUOTA,
                f"{provider} is rate-limiting CMF (too many requests or quota used up). "
                "Wait a minute and try again.",
            )
        if transient == "unavailable":
            return ClassifiedError(
                PROVIDER_UNAVAILABLE,
                f"{provider} is overloaded or unavailable right now. Try again in a minute.",
            )
    return None


def _embedding_mismatch(config) -> ClassifiedError:
    return ClassifiedError(
        EMBEDDING_MISMATCH,
        f"The embedding width doesn't match this graph (EMBEDDING_DIM={config.embedding_dim}, "
        f"embeddings from '{config.embed_provider}'). A graph keeps the width it was built with: either "
        "switch back to the embedder it was built with, or point FALKORDB_DATABASE at a new graph.",
    )
