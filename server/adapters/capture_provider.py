"""The LLM provider unattended capture workers extract with.

Every background worker (claude_code, claude_cowork, codex, antigravity)
runs its own reasoning-episode extraction with no human review gate in
front of it, and one long transcript is dozens of extraction calls. So the
workers don't inherit the interactive CMF_LLM_PROVIDER: they switch it to
CMF_CAPTURE_LLM_PROVIDER (default "local") for the duration of the pass.
"""

from __future__ import annotations

import contextlib
import os

from server.core.config import capture_llm_provider_from_env


@contextlib.contextmanager
def capture_llm_provider():
    """Set CMF_LLM_PROVIDER to the capture provider for the duration of the
    block, then restore whatever was there before -- never leaks into the
    caller's process-wide state.

    The setting is read and validated on entry, so a typo raises before any
    extraction call is made.
    """
    provider = capture_llm_provider_from_env()
    previous = os.environ.get("CMF_LLM_PROVIDER")
    os.environ["CMF_LLM_PROVIDER"] = provider
    try:
        yield provider
    finally:
        if previous is None:
            os.environ.pop("CMF_LLM_PROVIDER", None)
        else:
            os.environ["CMF_LLM_PROVIDER"] = previous
