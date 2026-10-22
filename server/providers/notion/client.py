"""Bounded Notion I/O behind the synchronous KnowledgeSource seam.

Socket timeouts alone do not bound a stream that keeps making progress. The
whole operation therefore has a deadline, including admission and cooldowns;
a daemon worker keeps that bound even when called inside an existing event
loop. Credential gates outlive provider instances because core recreates them.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
import hashlib
import json
import threading
import time
from typing import Any, Awaitable, Callable, TypeVar
from uuid import UUID

import httpx

from server.core.errors import ProviderConfigurationError

VERSION = "2026-03-11"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
T = TypeVar("T")


def error(message: str) -> ProviderConfigurationError:
    return ProviderConfigurationError(f"Notion: {message}")


def uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise error("invalid resource UUID in configuration or response") from None


@dataclass
class Budget:
    end: float
    cancelled: threading.Event = field(default_factory=threading.Event)

    def remaining(self) -> float:
        remaining = self.end - time.monotonic()
        if self.cancelled.is_set() or remaining <= 0:
            raise error("query deadline exceeded; other knowledge sources remain available")
        return remaining


@dataclass
class CredentialState:
    gate: threading.Lock = field(default_factory=threading.Lock)
    lock: threading.Lock = field(default_factory=threading.Lock)
    rate: int = 10
    next_start: float = 0.0
    cooldown: float = 0.0
    starts: deque[float] = field(default_factory=deque)
    warned: bool = False


_states: dict[tuple[str, object], CredentialState] = {}
_states_lock = threading.Lock()
# A single bounded LRU, rather than one unbounded cache for each workspace.
_cache: OrderedDict[tuple[object, ...], tuple[str, tuple[str, ...]]] = OrderedDict()
_cache_lock = threading.Lock()
_cache_epoch = 0
_page_epochs: dict[str, int] = {}


def state_for(token: str, namespace: object, rate: int) -> tuple[str, CredentialState]:
    fingerprint = hashlib.sha256(token.encode()).hexdigest()
    with _states_lock:
        state = _states.setdefault((fingerprint, namespace), CredentialState())
        with state.lock:
            state.rate = min(state.rate, rate)
    return fingerprint, state


def cache_get(key: tuple[object, ...]) -> tuple[str, tuple[str, ...]] | None:
    with _cache_lock:
        value = _cache.get(key)
        if value is not None:
            _cache.move_to_end(key)
        return value


def cache_drop(prefix: tuple[object, ...]) -> None:
    with _cache_lock:
        for key in list(_cache):
            if key[:-1] == prefix:
                del _cache[key]


def cache_epoch(page_id: str | None = None) -> int:
    with _cache_lock:
        return _cache_epoch if page_id is None else _page_epochs.get(page_id, 0)


def invalidate_page(page_id: str) -> None:
    global _cache_epoch
    with _cache_lock:
        _cache_epoch += 1
        _page_epochs[page_id] = _cache_epoch
        for key in list(_cache):
            if len(key) >= 2 and key[-2] == page_id:
                del _cache[key]


def cache_publish(entries: dict[tuple[object, ...], tuple[str, tuple[str, ...]]], budget: Budget, epoch: int | dict[str, int] | None = None) -> None:
    with _cache_lock:
        if isinstance(epoch, int) and epoch != _cache_epoch:
            return
        budget.remaining()
        for key, value in entries.items():
            if isinstance(epoch, dict) and epoch.get(key[-2]) != _page_epochs.get(key[-2], 0):
                continue
            for old in list(_cache):
                if old[:-1] == key[:-1]:
                    del _cache[old]
            _cache[key] = value
            while len(_cache) > 128:
                _cache.popitem(last=False)


def run_bounded(
    operation: Callable[[Budget], Awaitable[T]], state: CredentialState, seconds: float,
) -> T:
    budget = Budget(time.monotonic() + seconds)
    if not state.gate.acquire(timeout=budget.remaining()):
        raise error("query deadline exceeded waiting for another Notion query")
    result: Future[T] = Future()
    control_lock = threading.Lock()
    control: list[tuple[asyncio.AbstractEventLoop, asyncio.Task[Any]]] = []

    async def invoke() -> T:
        task = asyncio.current_task()
        assert task is not None
        with control_lock:
            control.append((asyncio.get_running_loop(), task))
        async with asyncio.timeout(budget.remaining()):
            return await operation(budget)

    def worker() -> None:
        try:
            value = asyncio.run(invoke())
            result.set_result(value)
        except BaseException as exc:
            # Never let request/transport reprs (which may include credentials)
            # escape into core's logger and errors dictionary.
            safe = exc if isinstance(exc, ProviderConfigurationError) else error(
                "query failed or timed out; check connectivity and integration configuration"
            )
            result.set_exception(safe)
        finally:
            state.gate.release()

    try:
        threading.Thread(target=worker, name="cmf-notion-query", daemon=True).start()
    except BaseException:
        state.gate.release()
        raise error("could not start query worker") from None
    try:
        return result.result(timeout=budget.remaining())
    except (FutureTimeout, ProviderConfigurationError) as exc:
        budget.cancelled.set()
        with control_lock:
            for loop, task in control:
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:  # worker already closed its loop
                    pass
        if isinstance(exc, ProviderConfigurationError):
            raise exc from None
        raise error("query deadline exceeded; check Notion access and connectivity") from None


class NotionClient:
    """One query's HTTP budget, sharing pacing and cooldown across instances.

    Only the fixed API origin receives authorization. Transport injection keeps
    the conformance suite entirely offline without a second code path.
    """

    def __init__(
        self, token: str, state: CredentialState, budget: Budget,
        transport: httpx.AsyncBaseTransport | None = None, *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.state, self.budget = state, budget
        self.clock, self.sleep = clock, sleep
        self.retries = 0
        self.http = httpx.AsyncClient(
            base_url="https://api.notion.com/v1/", transport=transport,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": VERSION},
            follow_redirects=False, trust_env=False,
        )

    async def pace(self) -> None:
        while True:
            remaining = self.budget.remaining()
            now = self.clock()
            with self.state.lock:
                while self.state.starts and self.state.starts[0] <= now - 60:
                    self.state.starts.popleft()
                target = max(self.state.next_start, self.state.cooldown)
                if len(self.state.starts) >= self.state.rate * 60:
                    target = max(target, self.state.starts[0] + 60)
                delay = max(0.0, target - now)
                if delay == 0:
                    self.state.starts.append(now)
                    self.state.next_start = now + 1 / self.state.rate
                    return
            if delay >= remaining:
                raise error("rate-limit wait exceeds query deadline; retry later")
            await self.sleep(delay)

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        while True:
            await self.pace()
            try:
                async with self.http.stream(
                    method, path, timeout=httpx.Timeout(min(5.0, self.budget.remaining())), **kwargs,
                ) as response:
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        self.budget.remaining()
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE_BYTES:
                            raise error("response exceeds bounded size")
                    status = response.status_code
                    retry_after = response.headers.get("Retry-After", "")
                self.budget.remaining()
                try:
                    payload = json.loads(data)
                except (ValueError, UnicodeError):
                    if status < 400:
                        raise error("invalid JSON response") from None
                    payload = {}
                if not isinstance(payload, dict):
                    raise error("invalid response object")
                if status in (429, 529):
                    reason = payload.get("additional_data", {})
                    if isinstance(reason, dict) and reason.get("rate_limit_reason") == "public_api_request_blocked":
                        raise error("API access blocked; contact Notion support")
                    delay = int(retry_after) if retry_after.isascii() and retry_after.isdecimal() else 2 ** self.retries
                    with self.state.lock:
                        self.state.cooldown = max(self.state.cooldown, self.clock() + delay)
                    if self.retries >= 2:
                        raise error("rate-limit retries exhausted; retry later")
                    self.retries += 1
                    continue
                if status >= 300:
                    if status in (401, 403, 404):
                        raise error(f"HTTP {status}; check token, Read content capability, and shared pages")
                    raise error(f"HTTP {status}; request failed")
                return payload
            except httpx.HTTPError:
                raise error("network failure; check connectivity") from None

    async def patch_once(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """One mutation attempt, with no retry on any outcome."""
        await self.pace()
        self.budget.remaining()
        async with self.http.stream("PATCH", path, json=payload,
                                    timeout=httpx.Timeout(min(5.0, self.budget.remaining()))) as response:
            if response.status_code in (429, 529):
                raw = response.headers.get("Retry-After", "")
                delay = int(raw) if raw.isascii() and raw.isdecimal() else 1
                with self.state.lock:
                    self.state.cooldown = max(self.state.cooldown, self.clock() + delay)
            data = bytearray()
            async for chunk in response.aiter_bytes():
                self.budget.remaining()
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise error("mutation response exceeds bounded size; reconcile outcome")
            if response.status_code >= 300:
                raise error(f"mutation HTTP {response.status_code}; reconcile outcome before retry")
        self.budget.remaining()
        value = json.loads(data)
        if not isinstance(value, dict):
            raise error("invalid mutation reply; reconcile outcome")
        return value

    async def pages(self, path: str, body: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _ in range(2):
            response = await self.request("POST", path, json=body)
            batch = response.get("results")
            if not isinstance(batch, list) or len(batch) > limit or not all(isinstance(x, dict) for x in batch):
                raise error("invalid paginated results")
            results.extend(batch)
            more = response.get("has_more")
            if not isinstance(more, bool):
                raise error("invalid pagination flag")
            if not more:
                break
            cursor = response.get("next_cursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise error("invalid or repeated pagination cursor")
            seen.add(cursor)
            usable = {uuid(x["id"]) for x in results if x.get("object") == "page" and not trashed(x)}
            if len(usable) >= limit:
                break
            body = {**body, "start_cursor": cursor}
        return results


def trashed(obj: dict[str, Any]) -> bool:
    return bool(obj.get("archived") or obj.get("in_trash"))
