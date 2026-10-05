"""One Spark-using job at a time (2026-10-02, docs/CLAUDE-HARNESS-PROVENANCE-PLAN.md).

Transcript pollers (claude_code, claude_cowork, antigravity, codex) run
ExtractPolicy consolidation on the local Spark LLM. Run alongside each other,
a backfill, or the MS9 Phase 4 wiki extraction, they swamp the host. A poll
that can't get the slot skips its whole pass *before tailing*, so no tail
offset advances and the next poll retries -- nothing is lost, only delayed.

The slot is an exclusive, non-blocking `fcntl.flock` on
imports/journal/spark_job.lock (codex's WorkerLock), plus a refusal while a
process running `extract_wiki_relationships.py` exists -- that script is not
modified to take the lock, so it is detected instead.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
import subprocess
from typing import Iterator, Optional

from server.adapters.codex.transcript_reader import WorkerLock
from server.journal.store import DEFAULT_JOURNAL_PATH

logger = logging.getLogger(__name__)

LOCK_NAME = "spark_job.lock"
# Spark jobs that don't take the lock themselves, detected by command line.
EXTERNAL_SPARK_JOBS = ("extract_wiki_relationships.py",)


def external_spark_job_running() -> Optional[str]:
    """The first known lock-less Spark job found running, or None."""
    if os.getenv("CMF_SPARK_LOCK_IGNORE_EXTERNAL"):  # tests
        return None
    for pattern in EXTERNAL_SPARK_JOBS:
        try:
            out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            return pattern
    return None


@contextlib.contextmanager
def spark_slot(journal_db: Optional[Path] = None, *, enabled: bool = True) -> Iterator[Optional[str]]:
    """Yield None when the Spark slot is held for the block, else the reason
    it isn't ("lock held by another job", or the external job's name).
    `enabled=False` (a journal-only run) always yields None without locking."""
    if not enabled:
        yield None
        return
    external = external_spark_job_running()
    if external:
        logger.info("spark slot: %s is running, skipping this pass", external)
        yield f"{external} running"
        return
    lock = WorkerLock(Path(journal_db or DEFAULT_JOURNAL_PATH).parent / LOCK_NAME)
    if not lock.acquire():
        logger.info("spark slot: another Spark job holds %s, skipping this pass", LOCK_NAME)
        yield "lock held by another job"
        return
    try:
        yield None
    finally:
        lock.release()
