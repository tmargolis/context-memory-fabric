"""Capture middleware: journal a source event per MCP tool call (MS4a).

Design constraints, in priority order:

1. Never add latency or failure risk to the tool call itself. `call_next(ctx)`
   runs and its result is returned before any capture work starts; capture
   work is wrapped in a broad try/except that logs rather than raises.
2. Never block the client on journal I/O. Capture pushes onto a bounded
   in-process asyncio.Queue (`put_nowait`) and a single background consumer
   task drains it into the journal. If the queue is full, the new event is
   dropped and counted (server.capture.health) — documented drop policy:
   newest-drop, not oldest-evict, so a burst never silently reorders what
   little does get journaled.
3. No consolidation is triggered from here. server.consolidation.pipeline's
   run_consolidation() is pull-based — it queries the journal itself for
   unprocessed events on its own schedule (CLI, a future scheduled task).
   Capture's only job is getting events into the journal reliably; nothing
   here needs to know that a consolidation pipeline exists at all.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import time
from typing import Any, Optional

from server.capture import filters, identity
from server.capture.health import CaptureHealth, get_default_health
from server.core.models import DatePrecision, SourceEvent, SourceProvenance
from server.journal.identity import compute_content_hash, compute_event_id
from server.journal.store import SqliteEventStore

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"
EVENT_TYPE_TOOL_CALL = "mcp_tool_call"

_RESULT_SUMMARY_MAX_CHARS = 1000


def _summarize_result(result: Any) -> str:
    try:
        text = str(result)
    except Exception:
        text = "<unrepresentable result>"
    if len(text) > _RESULT_SUMMARY_MAX_CHARS:
        return text[:_RESULT_SUMMARY_MAX_CHARS] + "...[truncated]"
    return text


def _client_info_for(session: Any) -> Optional[Any]:
    client_params = getattr(session, "client_params", None)
    return getattr(client_params, "client_info", None) if client_params is not None else None


def build_tool_call_event(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: Any,
    session: Any,
    request_id: Any,
) -> Optional[SourceEvent]:
    """Construct the SourceEvent for one captured tool call, or None if filtered out."""
    client_info = _client_info_for(session)
    harness = identity.resolve_harness(client_info)

    if not filters.should_capture(tool_name, harness):
        return None

    session_id = identity.resolve_session_id(session)
    redacted_args, redacted_count = filters.redact_secrets_and_count(arguments or {})

    content = {
        "tool": tool_name,
        "arguments": redacted_args,
        "result_summary": _summarize_result(result),
    }
    content_hash = compute_content_hash(content)
    event_id = compute_event_id(
        harness=harness,
        content_hash=content_hash,
        conversation_id=session_id,
        turn_id=str(request_id) if request_id is not None else None,
        namespace="mcp_capture" if request_id is None else None,
    )

    event = SourceEvent(
        schema_version=SCHEMA_VERSION,
        event_id=event_id,
        event_type=EVENT_TYPE_TOOL_CALL,
        source=SourceProvenance(
            harness=harness,
            conversation_id=session_id,
            session_id=session_id,
            turn_id=str(request_id) if request_id is not None else None,
        ),
        observed_at=datetime.now(timezone.utc),
        content=content,
        content_hash=content_hash,
        actor_type="user",
        date_precision=DatePrecision.NONE,
        metadata={
            "client_version": identity.client_version(client_info),
            "redacted_field_count": redacted_count,
        },
    )
    return event, redacted_count


class CaptureQueueFull(Exception):
    """Internal signal only — never raised across the middleware boundary."""


class CaptureMiddleware:
    """`ServerMiddleware`-shaped capture hook (server.mcp registers it via `app.middleware.append(...)`).

    One instance per server process. Owns the bounded queue, the background
    consumer task (lazily started on first use — there is no server
    lifespan hook this module needs to attach to), and the journal store
    connection (accessed only from the consumer's own asyncio task, so
    single-threaded for SQLite's sake — see the module using it).
    """

    def __init__(
        self,
        event_store: Optional[SqliteEventStore] = None,
        health: Optional[CaptureHealth] = None,
        queue_maxsize: int = 500,
    ) -> None:
        self._event_store = event_store or SqliteEventStore()
        self._health = health or get_default_health()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_maxsize)
        self._consumer_task: Optional[asyncio.Task] = None
        self._health.set_queue_depth_source(lambda: self._queue.qsize())

    def _ensure_consumer_started(self) -> None:
        if self._consumer_task is None or self._consumer_task.done():
            self._consumer_task = asyncio.create_task(self._consume_forever())

    async def _consume_forever(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                self._event_store.append(event)
            except Exception:
                logger.exception("Capture consumer failed to journal event %s", getattr(event, "event_id", "?"))
                self._health.record_error()
            finally:
                self._queue.task_done()

    def enqueue(self, event: SourceEvent, redacted_count: int) -> None:
        """Synchronous, non-blocking enqueue. Safe to call from a running event loop."""
        self._ensure_consumer_started()
        try:
            self._queue.put_nowait(event)
            self._health.record_captured()
            self._health.record_redacted(redacted_count)
        except asyncio.QueueFull:
            self._health.record_dropped("queue_full")
            logger.warning("Capture queue full — dropped event for tool call (event_id=%s)", event.event_id)

    async def _capture_tool_call(self, tool_name: str, arguments: dict[str, Any], result: Any, session: Any, request_id: Any) -> None:
        try:
            built = build_tool_call_event(
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                session=session,
                request_id=request_id,
            )
            if built is None:
                return
            event, redacted_count = built
            self.enqueue(event, redacted_count)
        except Exception:
            # Capture must never surface an exception to the caller (a
            # fire-and-forget task has no caller to surface it to anyway,
            # but this guards the __call__ path too if ever invoked inline).
            logger.exception("Capture failed for tool call %r", tool_name)
            self._health.record_error()

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        # Logged here (not in capture) because this fires synchronously and
        # unconditionally on every tool call, independent of the journal's
        # own filtering/health-queue path -- see build_tool_call_event's
        # `should_capture` gate below, which can skip journaling a call this
        # log line still shows. logging.basicConfig's format (server.mcp's
        # main()) supplies the timestamp; nothing extra needed here.
        log_tool_name = None
        if ctx.method == "tools/call":
            log_tool_name = (ctx.params or {}).get("name")
        start = time.monotonic() if log_tool_name else None
        if log_tool_name:
            logger.info("TOOL CALL START: %s", log_tool_name)

        result = await call_next(ctx)

        if log_tool_name:
            logger.info("TOOL CALL DONE:  %s (%.1fs)", log_tool_name, time.monotonic() - start)

        if ctx.request_id is None or ctx.method != "tools/call":
            return result

        params = ctx.params or {}
        tool_name = params.get("name")
        if not tool_name:
            return result

        arguments = params.get("arguments") or {}
        # Fire-and-forget: scheduled as a task, not awaited, so a slow or
        # failing capture can never add latency to a response the client
        # already has.
        asyncio.create_task(
            self._capture_tool_call(tool_name, arguments, result, ctx.session, ctx.request_id)
        )
        return result


_DEFAULT_MIDDLEWARE: Optional[CaptureMiddleware] = None


def get_default_capture_middleware() -> CaptureMiddleware:
    global _DEFAULT_MIDDLEWARE
    if _DEFAULT_MIDDLEWARE is None:
        _DEFAULT_MIDDLEWARE = CaptureMiddleware()
    return _DEFAULT_MIDDLEWARE
