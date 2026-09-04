"""In-process capture-health counters (MS4a): captured, dropped, redacted, queue depth.

Process-local, in-memory, reset on server restart — this is an operational
signal for "is capture currently keeping up," not a durable metric store.
Durable counts (how many events actually made it into the journal) are
always available from the journal itself via server.journal.store.SqliteEventStore.stats().
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional


class CaptureHealth:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._captured = 0
        self._dropped = 0
        self._redacted_fields = 0
        self._errors = 0
        self._drop_reasons: dict[str, int] = {}
        self._queue_depth_fn: Optional[Callable[[], int]] = None

    def set_queue_depth_source(self, fn: Callable[[], int]) -> None:
        """Register a callable returning the live queue depth for status reporting."""
        self._queue_depth_fn = fn

    def record_captured(self) -> None:
        with self._lock:
            self._captured += 1

    def record_dropped(self, reason: str) -> None:
        with self._lock:
            self._dropped += 1
            self._drop_reasons[reason] = self._drop_reasons.get(reason, 0) + 1

    def record_redacted(self, field_count: int) -> None:
        if field_count <= 0:
            return
        with self._lock:
            self._redacted_fields += field_count

    def record_error(self) -> None:
        with self._lock:
            self._errors += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "captured": self._captured,
                "dropped": self._dropped,
                "drop_reasons": dict(self._drop_reasons),
                "redacted_fields": self._redacted_fields,
                "errors": self._errors,
                "queue_depth": self._queue_depth_fn() if self._queue_depth_fn else None,
            }


_DEFAULT_HEALTH: Optional[CaptureHealth] = None
_DEFAULT_HEALTH_LOCK = threading.Lock()


def get_default_health() -> CaptureHealth:
    global _DEFAULT_HEALTH
    with _DEFAULT_HEALTH_LOCK:
        if _DEFAULT_HEALTH is None:
            _DEFAULT_HEALTH = CaptureHealth()
        return _DEFAULT_HEALTH


def format_health_report(snapshot: dict[str, Any]) -> str:
    lines = [
        "### Capture Health",
        f"- **Captured:** {snapshot['captured']}",
        f"- **Dropped:** {snapshot['dropped']}",
    ]
    if snapshot["drop_reasons"]:
        for reason, count in snapshot["drop_reasons"].items():
            lines.append(f"  - `{reason}`: {count}")
    lines.append(f"- **Redacted fields (cumulative):** {snapshot['redacted_fields']}")
    lines.append(f"- **Errors:** {snapshot['errors']}")
    queue_depth = snapshot["queue_depth"]
    lines.append(f"- **Queue depth:** {queue_depth if queue_depth is not None else 'unavailable'}")
    return "\n".join(lines)
