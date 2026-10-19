"""One local-LLM job at a time (2026-10-02, docs/CLAUDE-HARNESS-PROVENANCE-PLAN.md;
provider-aware since 2026-10-19).

Capture extraction, backfills, the MS9 Phase 4 wiki extraction and an
edit_memory re-extract all run on the same local LLM host. Run alongside each
other they swamp it, so they take one slot: an exclusive, non-blocking file
lock (server.adapters.file_lock) on imports/journal/spark_job.lock. A pass that
can't get the slot skips *before tailing*, so no tail offset advances and the
next pass retries -- nothing is lost, only delayed.

The slot exists only to protect a local host. When neither the capture
provider (CMF_CAPTURE_LLM_PROVIDER) nor the interactive provider
(CMF_LLM_PROVIDER) is "local", a hosted API takes the concurrency and the slot
is a no-op. CMF_LLM_SLOT=always|never overrides that (e.g. a self-hosted
OpenAI-compatible endpoint configured as "openai").

Until 2026-10-19 the wiki extraction was detected with `pgrep -f
extract_wiki_relationships.py`. Pollers firing in the same second matched each
other's pgrep, and every pass was skipped for ~19 hours. The script now takes
the slot itself, so nothing is matched by process name.

The slot is re-entrant within a process: the capture runner
(server.adapters.capture_runner) holds it for a whole pass, and each adapter's
own process_pending, which also asks for it, is then let straight through.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
import sys
from typing import Iterator, Optional

from server.adapters.file_lock import WorkerLock
from server.core.config import LOCAL_PROVIDER, _provider, capture_llm_provider_from_env
from server.journal.store import DEFAULT_JOURNAL_PATH

logger = logging.getLogger(__name__)

LOCK_NAME = "spark_job.lock"
HOLDER_SUFFIX = ".holder"
BUSY_REASON = "lock held by another job"

_held_depth = 0  # >0 while this process holds the slot


def slot_required() -> bool:
    """Whether the slot is enforced, from CMF_LLM_SLOT (auto|always|never)."""
    mode = (os.getenv("CMF_LLM_SLOT") or "auto").strip().lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    if mode != "auto":
        raise ValueError(f"CMF_LLM_SLOT={mode!r} is not recognised. Valid values are auto, always, never.")
    return LOCAL_PROVIDER in (capture_llm_provider_from_env(), _provider("CMF_LLM_PROVIDER"))


def _lock_path(journal_db: Optional[Path]) -> Path:
    return Path(journal_db or DEFAULT_JOURNAL_PATH).parent / LOCK_NAME


def _holder(lock_path: Path) -> Optional[str]:
    """`pid N (script)` of the job that wrote the holder file, if it's still alive."""
    try:
        pid_text, _, command = (lock_path.parent / (lock_path.name + HOLDER_SUFFIX)).read_text().partition(" ")
        pid = int(pid_text)
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    return f"pid {pid} ({command.strip() or '?'})"


def _write_holder(lock_path: Path) -> None:
    script = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else "python"
    if script in ("__main__.py", "-m", "-c", "") and len(sys.argv) > 1:
        script = sys.argv[1]
    try:
        (lock_path.parent / (lock_path.name + HOLDER_SUFFIX)).write_text(f"{os.getpid()} {script}")
    except OSError:
        pass


@contextlib.contextmanager
def llm_slot(journal_db: Optional[Path] = None, *, enabled: bool = True) -> Iterator[Optional[str]]:
    """Yield None when the block may use the local LLM, else why it may not
    ("lock held by another job", plus the holder when known).
    `enabled=False` (a journal-only run), a non-local provider, or a slot this
    process already holds always yields None without locking."""
    global _held_depth
    if not enabled or _held_depth or not slot_required():
        _held_depth += 1
        try:
            yield None
        finally:
            _held_depth -= 1
        return
    path = _lock_path(journal_db)
    lock = WorkerLock(path)
    if not lock.acquire():
        holder = _holder(path)
        reason = f"{BUSY_REASON}: {holder}" if holder else BUSY_REASON
        logger.info("llm slot: %s, skipping this pass", reason)
        yield reason
        return
    _write_holder(path)
    _held_depth += 1
    try:
        yield None
    finally:
        _held_depth -= 1
        lock.release()


spark_slot = llm_slot  # the name every caller used before 2026-10-19
