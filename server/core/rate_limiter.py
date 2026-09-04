"""Hard, persistent rate gate for Gemini Developer API calls (MS4a).

Purpose: guarantee CMF never places a Gemini call that would exceed the
free tier's per-model RPM/RPD ceilings, so consolidation cannot incur spend
regardless of whether a billing account happens to be attached to the API
key's project. This is a local ledger, not a query against Google's actual
usage counters — see the GeminiRateLimiter docstring for what that implies.

KNOWN_MODEL_BUDGETS below is a live snapshot Todd read from his own AI
Studio rate-limit dashboard (aistudio.google.com/rate-limit) on 2026-09-04.
Google does not publish these figures anywhere static (ai.google.dev's own
rate-limits page explicitly declines to and points at the dashboard
instead), and they can change as Google adjusts free-tier policy or Todd's
account tier changes. Re-confirm against the dashboard periodically; do not
assume these numbers are still accurate deep into the future.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_PACIFIC = ZoneInfo("America/Los_Angeles")
_RPM_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class ModelBudget:
    """Free-tier ceilings for one Gemini model, as shown in AI Studio."""

    model: str
    rpm: int
    tpm: int
    rpd: int


# Source: Todd's AI Studio "Free tier" / "Context Memory Fabric" project
# dashboard, 2026-09-04. Only models plausibly useful for CMF's structured
# extraction, reranking, and embedding calls are included — image/video/TTS/
# Live API rows on the dashboard are irrelevant here and omitted. Pro-tier
# models are omitted deliberately: the dashboard showed 0/0/0 for every Pro
# variant, meaning the free tier grants no Pro access at all right now.
KNOWN_MODEL_BUDGETS: dict[str, ModelBudget] = {
    "gemini-3.8-flash": ModelBudget("gemini-3.8-flash", rpm=5, tpm=250_000, rpd=20),
    "gemini-3.5-flash-lite": ModelBudget("gemini-3.5-flash-lite", rpm=15, tpm=250_000, rpd=500),
    "gemini-3.1-flash-lite": ModelBudget("gemini-3.1-flash-lite", rpm=15, tpm=250_000, rpd=500),
    "gemini-2.5-flash-lite": ModelBudget("gemini-2.5-flash-lite", rpm=10, tpm=250_000, rpd=20),
    "gemini-embedding-001": ModelBudget("gemini-embedding-001", rpm=100, tpm=30_000, rpd=1_000),
}

DEFAULT_MODEL_CHAIN = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
DEFAULT_CALLS_PER_OPERATION = 3  # graphiti_core's add_episode()/search() are
# black boxes that may issue more than one underlying LLM call per
# invocation (extraction + reflection passes, etc.) with no way for CMF to
# observe the real count. Reserving a multiple of 1 per operation is a
# deliberate over-estimate so the local ledger stays conservative relative
# to Google's actual counters rather than risking silent overrun.


class GeminiQuotaExhaustedError(RuntimeError):
    """Raised when every model in the configured chain lacks headroom.

    Callers (server.providers.memory_graphiti.remember/recall today; MS4a's
    consolidation queue later) should treat this as "pause consolidation,
    the journal write already happened" rather than an unexpected failure.
    """


def _date_str(epoch_seconds: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(epoch_seconds, tz=_PACIFIC).strftime("%Y-%m-%d")


def _default_state_path() -> Path:
    project_root = Path(__file__).resolve().parent.parent.parent
    return project_root / "imports" / "state" / "gemini_rate_limiter_state.json"


class GeminiRateLimiter:
    """Local, persisted ledger enforcing free-tier RPM/TPM/RPD per model.

    This tracks CMF's own request history against KNOWN_MODEL_BUDGETS. It
    does NOT read Google's real usage counters — there is no supported API
    for that, only the AI Studio dashboard a human reads. Two consequences
    follow, both accepted as the cost of guaranteeing zero surprise spend
    without a live quota-read API:

    1. If something other than this CMF process also calls the same API
       key/model (e.g. manual AI Studio use), this ledger will not see that
       usage and could reserve a call Google then rejects with a real 429.
       remember()/recall()'s existing retry-with-backoff logic is the
       backstop for that case; it is not this class's job to eliminate it.
    2. Restarting the process keeps the ledger (state is persisted to
       state_path, not held only in memory), so a restart mid-day does not
       reset the day's RPD count to zero.

    Thread-safety: a single process-local lock guards read-modify-write of
    the state file, sufficient for CMF's single-process MCP server today.
    """

    def __init__(
        self,
        chain: list[str],
        budgets: dict[str, ModelBudget],
        state_path: Path,
        calls_per_operation: int = DEFAULT_CALLS_PER_OPERATION,
    ) -> None:
        if not chain:
            raise ValueError("Model chain must contain at least one model.")
        unknown = [m for m in chain if m not in budgets]
        if unknown:
            raise ValueError(
                f"Model(s) {unknown} in the chain have no known budget in KNOWN_MODEL_BUDGETS. "
                "Add them (with real numbers read from AI Studio) before including them in the chain."
            )
        self._chain = list(chain)
        self._budgets = budgets
        self._state_path = state_path
        self._calls_per_operation = max(1, calls_per_operation)
        self._lock = threading.Lock()

    @property
    def chain(self) -> list[str]:
        """The configured model preference chain, in order."""
        return list(self._chain)

    # -- state I/O -----------------------------------------------------

    def _load(self) -> dict:
        if not self._state_path.exists():
            return {"day": None, "models": {}}
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not read {self._state_path}, starting a fresh ledger: {e}")
            return {"day": None, "models": {}}

    def _save(self, state: dict) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        tmp_path.replace(self._state_path)

    @staticmethod
    def _model_bucket(state: dict, model: str) -> dict:
        return state.setdefault("models", {}).setdefault(
            model, {"rpd_count": 0, "minute_requests": [], "minute_tokens": []}
        )

    def _prune_minute_window(self, bucket: dict, now: float) -> None:
        cutoff = now - _RPM_WINDOW_SECONDS
        bucket["minute_requests"] = [t for t in bucket["minute_requests"] if t >= cutoff]
        bucket["minute_tokens"] = [pair for pair in bucket["minute_tokens"] if pair[0] >= cutoff]

    def _roll_day_if_needed(self, state: dict, now: float) -> None:
        today = _date_str(now)
        if state.get("day") != today:
            state["day"] = today
            state["models"] = {}

    # -- public API ------------------------------------------------------

    def reserve(self, estimated_calls: Optional[int] = None, now: Optional[float] = None) -> str:
        """Reserve headroom for one operation, returning the model to use.

        Walks the chain in preference order and commits the reservation
        against the first model with RPM/RPD headroom for `estimated_calls`
        (defaults to calls_per_operation). Raises GeminiQuotaExhaustedError
        if no model in the chain has room.
        """
        calls = estimated_calls if estimated_calls is not None else self._calls_per_operation
        now = now if now is not None else time.time()

        with self._lock:
            state = self._load()
            self._roll_day_if_needed(state, now)

            for model in self._chain:
                budget = self._budgets[model]
                bucket = self._model_bucket(state, model)
                self._prune_minute_window(bucket, now)

                would_be_rpd = bucket["rpd_count"] + calls
                would_be_rpm = len(bucket["minute_requests"]) + calls
                if would_be_rpd > budget.rpd or would_be_rpm > budget.rpm:
                    continue

                bucket["rpd_count"] = would_be_rpd
                bucket["minute_requests"].extend([now] * calls)
                self._save(state)
                return model

            self._save(state)  # persist the day-roll/pruning even on exhaustion
            raise GeminiQuotaExhaustedError(
                f"No model in chain {self._chain} has free-tier headroom for "
                f"{calls} call(s) right now. Capture should continue to the "
                "journal; consolidation must wait for the next RPM window or "
                "day boundary (midnight Pacific)."
            )

    def status(self, now: Optional[float] = None) -> dict:
        """Report current usage vs. ceiling for every model in the chain."""
        now = now if now is not None else time.time()
        with self._lock:
            state = self._load()
            self._roll_day_if_needed(state, now)
            report = {"day": state.get("day"), "models": {}}
            for model in self._chain:
                budget = self._budgets[model]
                bucket = self._model_bucket(state, model)
                self._prune_minute_window(bucket, now)
                report["models"][model] = {
                    "rpm_used": len(bucket["minute_requests"]),
                    "rpm_limit": budget.rpm,
                    "rpd_used": bucket["rpd_count"],
                    "rpd_limit": budget.rpd,
                }
            self._save(state)
            return report


_DEFAULT_LIMITER: Optional[GeminiRateLimiter] = None
_DEFAULT_LIMITER_LOCK = threading.Lock()


def get_default_rate_limiter() -> GeminiRateLimiter:
    """Singleton limiter configured from CMF_GEMINI_MODEL_CHAIN / CMF_GEMINI_CALLS_PER_OPERATION.

    Env vars (both optional):
    - CMF_GEMINI_MODEL_CHAIN: comma-separated model ids in preference order.
      Default: "gemini-3.5-flash-lite,gemini-3.1-flash-lite".
    - CMF_GEMINI_CALLS_PER_OPERATION: int, default 3 (see module docstring
      on DEFAULT_CALLS_PER_OPERATION for why this is a deliberate overestimate).
    """
    global _DEFAULT_LIMITER
    with _DEFAULT_LIMITER_LOCK:
        if _DEFAULT_LIMITER is None:
            chain_env = os.getenv("CMF_GEMINI_MODEL_CHAIN")
            chain = [m.strip() for m in chain_env.split(",") if m.strip()] if chain_env else list(DEFAULT_MODEL_CHAIN)
            calls_env = os.getenv("CMF_GEMINI_CALLS_PER_OPERATION")
            calls_per_op = int(calls_env) if calls_env else DEFAULT_CALLS_PER_OPERATION
            _DEFAULT_LIMITER = GeminiRateLimiter(
                chain=chain,
                budgets=KNOWN_MODEL_BUDGETS,
                state_path=_default_state_path(),
                calls_per_operation=calls_per_op,
            )
        return _DEFAULT_LIMITER


def reset_default_rate_limiter() -> None:
    """Test-only: drop the cached singleton so the next call re-reads env."""
    global _DEFAULT_LIMITER
    with _DEFAULT_LIMITER_LOCK:
        _DEFAULT_LIMITER = None
