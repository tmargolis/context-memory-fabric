"""A non-blocking, process-level file lock that works on macOS, Linux and
Windows (MS10a task 4).

The pollers, hooks and the shared Spark slot (server.adapters.spark_lock)
all serialize through one of these. It used `fcntl.flock`, which doesn't
exist on Windows, so importing any poller failed there. POSIX keeps
`fcntl.flock`; Windows locks the file's first byte with `msvcrt.locking`.
Either lock is released by the OS when the process exits, so a killed
poller never leaves the slot held.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time
from typing import Optional

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class WorkerLock:
    """Non-blocking process-level file lock.

    Prevents concurrent execution between scheduled poller runs and hook
    invocations.
    """

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = Path(lock_path)
        self._fd: Optional[int] = None

    def acquire(self, blocking: bool = False) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            _lock(self._fd, blocking)
            return True
        except OSError:
            os.close(self._fd)
            self._fd = None
            return False

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            _unlock(self._fd)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    def __enter__(self) -> bool:
        return self.acquire(blocking=False)

    def __exit__(self, *exc_info: object) -> None:
        self.release()


if sys.platform == "win32":

    def _lock(fd: int, blocking: bool) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if not blocking:
                    raise
                time.sleep(0.5)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:

    def _lock(fd: int, blocking: bool) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
