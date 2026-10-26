"""How many tokens the extraction model can take, so prompts are sized to fit.

B18/B20 (docs/plan-active.md): poller extraction and `remember` sent prompts of
12-16K tokens to a Spark model loaded with an 8K context, and every one failed
with `n_keep >= n_ctx`. The limit belongs to whatever model the user has
loaded, so it is read rather than assumed:

1. CMF_LLM_CONTEXT_TOKENS, when set;
2. for the local provider, LM Studio's `loaded_context_length` for the
   configured model (`GET <base>/api/v0/models/<model>`), cached a few minutes;
3. otherwise a default: hosted models take far more than CMF ever sends, and
   a local server that doesn't report its context gets a conservative guess.

Token counts here are estimates from character counts (CHARS_PER_TOKEN errs
toward more tokens than the model's own tokenizer counts), never exact.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 3
HOSTED_DEFAULT_TOKENS = 128_000
LOCAL_UNKNOWN_TOKENS = 16_384
# Below this, Status warns that the local model is loaded too small for CMF.
RECOMMENDED_MIN_TOKENS = 16_384
_CACHE_SECONDS = 300.0

_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[float, Optional[int]]] = {}


def estimate_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def reply_reserve(limit: int) -> int:
    """Tokens left for the reply: reasoning models spend thousands thinking first."""
    return max(1024, min(8192, limit // 4))


def prompt_budget_chars(limit: Optional[int] = None) -> int:
    """Characters a prompt may use under `limit` (default: the configured model's)."""
    limit = limit if limit is not None else context_tokens()
    return max(0, limit - reply_reserve(limit)) * CHARS_PER_TOKEN


def loaded_context(base_url: str, model: str, timeout: float = 3.0) -> Optional[int]:
    """LM Studio's loaded context for `model`, or None if it doesn't say (cached)."""
    key = (base_url, model)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_SECONDS:
            return hit[1]
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    url = f"{root}/api/v0/models/{urllib.parse.quote(model, safe='')}"
    value: Optional[int] = None
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - the operator's own LLM server
            info = json.loads(resp.read().decode("utf-8"))
        raw = info.get("loaded_context_length")
        value = int(raw) if raw else None
    except Exception:  # noqa: BLE001 - not LM Studio, unreachable, or not loaded: fall back
        logger.debug("No loaded context length from %s", url, exc_info=True)
    with _lock:
        _cache[key] = (now, value)
    return value


def context_tokens(config=None) -> int:
    """The extraction model's context in tokens (see the module docstring)."""
    override = os.environ.get("CMF_LLM_CONTEXT_TOKENS", "").strip()
    if override:
        try:
            return max(1024, int(override))
        except ValueError:
            logger.warning("CMF_LLM_CONTEXT_TOKENS=%r is not a number; ignoring it.", override)
    if config is None:
        from server.core.config import load_config

        config = load_config()
    if not config.llm_is_local:
        return HOSTED_DEFAULT_TOKENS
    return loaded_context(config.local_llm_base_url, config.local_llm_model) or LOCAL_UNKNOWN_TOKENS


def clear_cache() -> None:
    with _lock:
        _cache.clear()
