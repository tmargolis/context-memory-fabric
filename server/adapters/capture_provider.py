"""The LLM providers unattended capture workers extract with.

Every background worker (claude_code, claude_cowork, codex, antigravity)
runs its own reasoning-episode extraction with no human review gate in
front of it. They extract with capture_llm_providers_from_env(): the shared
ranked list, or an older install's CMF_CAPTURE_LLM_PROVIDER. Extraction
reads CMF_LLM_PROVIDERS first and CMF_LLM_PROVIDER only when the list is
unset, so both are switched for the duration of the pass; switching only the
single variable left a ranked list in charge (fixed in v0.1.2).
"""

from __future__ import annotations

import contextlib
import os

from server.core.config import capture_llm_providers_from_env

_SWITCHED = ("CMF_LLM_PROVIDERS", "CMF_LLM_PROVIDER")


@contextlib.contextmanager
def capture_llm_provider():
    """Point CMF_LLM_PROVIDERS / CMF_LLM_PROVIDER at the capture providers
    for the duration of the block, then restore whatever was there before --
    never leaks into the caller's process-wide state. Yields the first one.

    The settings are read and validated on entry, so a typo raises before any
    extraction call is made.
    """
    providers = capture_llm_providers_from_env()
    previous = {key: os.environ.get(key) for key in _SWITCHED}
    os.environ["CMF_LLM_PROVIDERS"] = ",".join(providers)
    os.environ["CMF_LLM_PROVIDER"] = providers[0]
    try:
        yield providers[0]
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
