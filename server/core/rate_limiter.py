"""Hard, persistent rate gate for Gemini Developer API calls (MS4a).

Purpose: guarantee CMF never places a Gemini call that would exceed the
free tier's per-model RPM/RPD ceilings, so consolidation cannot incur spend
regardless of whether a billing account happens to be attached to the API
key's project. This is a local ledger, not a query against Google's actual
usage counters — see the GeminiRateLimiter docstring for what that implies.

KNOWN_MODEL_BUDGETS below is a live snapshot the user read from his own AI
Studio rate-limit dashboard (aistudio.google.com/rate-limit) on 2026-09-04.
Google does not publish these figures anywhere static (ai.google.dev's own
rate-limits page explicitly declines to and points at the dashboard
instead), and they can change as Google adjusts free-tier policy or the user's
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


# Source: the user's AI Studio "Free tier" / "Context Memory Fabric" project
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


# Transient-error classification, originally local to
# server.providers.memory_graphiti's remember()/recall() retry loops, moved
# here so server.consolidation.promotion can reach it too without importing
# a provider-internal name: `promote_reviewed`'s wait-and-retry treatment
# for GeminiQuotaExhaustedError (this LOCAL ledger's own pre-emptive block)
# needs the identical treatment for a REAL Gemini API 429/503 that survived
# remember()'s own bounded retry budget and reached promote_reviewed as a
# plain exception — a distinct failure path this ledger cannot see coming,
# since the local reservation believed there was headroom when the call was
# allowed through. memory_graphiti re-exports these names unchanged so
# every existing call site keeps working without modification.
#
# "429"/"resource_exhausted"/"quota"/"rate limit" catch real quota errors
# (which get_graphiti_for_operation()'s rate-limit reservation should mostly
# prevent from happening at all, but a call outside this process's own
# ledger — e.g. concurrent AI Studio usage — can still trigger one).
# "503"/"unavailable"/"high demand" catch transient server-side capacity
# errors, unrelated to quota, observed independently on more than one
# Gemini model during MS4a's own testing. Neither category implies a
# specific model is permanently broken; both are worth a backoff retry.
TRANSIENT_ERROR_MARKERS = {
    "quota": ("429", "resource_exhausted", "quota", "rate limit"),
    # "model unloaded" is LM Studio's, not Google's: with Auto-Evict enabled a
    # request to a different model can evict the one mid-generation, and the
    # in-flight call dies with {"error": "Model unloaded."}. Observed live on
    # the Spark. It is not an HTTP 5xx and matches none of the other markers,
    # so without this entry it surfaces as a hard failure and aborts a batch
    # run that a single retry would have carried through.
    "unavailable": (
        "503",
        "unavailable",
        "high demand",
        "overloaded",
        "model unloaded",
        # Carried over from ReasoningEpisodePolicyV1's own marker tuple when
        # that was folded into this shared classifier; Gemini uses this
        # phrasing for capacity pushback.
        "try again later",
    ),
}


# Billing failures on a paid API key (MS10a): an unfunded Anthropic or OpenAI
# account. Waiting doesn't fix these, and OpenAI's `insufficient_quota`
# contains "quota", so they are checked first and never treated as
# transient: the call fails at once with the provider's own "add credits"
# message instead of sleeping through retries.
BILLING_ERROR_MARKERS = (
    "insufficient_quota",
    "credit_balance_exhausted",
    "no credits remaining",
    "credit balance is too low",
    "purchase credits",
)


def is_billing_error(exc: Exception) -> bool:
    err_msg = str(exc).lower()
    return any(marker in err_msg for marker in BILLING_ERROR_MARKERS)


def classify_transient_error(exc: Exception) -> Optional[str]:
    if is_billing_error(exc):
        return None
    err_msg = str(exc).lower()
    for label, markers in TRANSIENT_ERROR_MARKERS.items():
        if any(marker in err_msg for marker in markers):
            return label
    return None


def is_transient_gemini_error(exc: Exception) -> bool:
    return classify_transient_error(exc) is not None


def _date_str(epoch_seconds: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(epoch_seconds, tz=_PACIFIC).strftime("%Y-%m-%d")


def _seconds_until_next_pacific_midnight(epoch_seconds: float) -> float:
    from datetime import datetime, timedelta

    now = datetime.fromtimestamp(epoch_seconds, tz=_PACIFIC)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now).total_seconds()


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
        unmetered: bool = False,
    ) -> None:
        if not chain:
            raise ValueError("Model chain must contain at least one model.")
        if not unmetered:
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
        self._unmetered = unmetered
        self._lock = threading.Lock()

    @property
    def unmetered(self) -> bool:
        """True when this limiter enforces nothing (local inference).

        Not implemented as "budgets so large they never bind": that would
        still load, mutate and re-save the JSON ledger on every reserve(),
        and a 1,243-episode backfill issues thousands of reservations. An
        unmetered limiter does no disk I/O at all.
        """
        return self._unmetered

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
        if self._unmetered:
            return self._chain[0]

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

    def reserve_model(self, model: str, calls: int = 1, now: Optional[float] = None) -> None:
        """Reserve headroom against one *named* model, with no chain fallback.

        `reserve()` answers "which model should I use", walking the chain
        until one has room. That question is meaningless for the embedder:
        there is exactly one embedding model and nothing to fall through to,
        so the only useful answer is "yes" or GeminiQuotaExhaustedError.

        This exists because the embedding quota is the ceiling CMF actually
        hits. A measured `add_episode` issues 3 generation calls and ~20
        embedding calls -- every entity name and edge fact is embedded
        individually -- so the free tier's 1,000/day embedding limit caps
        throughput near 50 episodes/day, well below what the 500 RPD
        generation limit implies. Until this method existed nothing counted
        those calls at all, and the ledger reported ample headroom right up
        to a live 429.
        """
        if self._unmetered:
            return

        budget = self._budgets.get(model)
        if budget is None:
            raise ValueError(
                f"No known budget for {model!r}; refusing to meter a model whose ceilings "
                "are unknown. Add it to KNOWN_MODEL_BUDGETS with real numbers first."
            )

        now = now if now is not None else time.time()
        with self._lock:
            state = self._load()
            self._roll_day_if_needed(state, now)
            bucket = self._model_bucket(state, model)
            self._prune_minute_window(bucket, now)

            would_be_rpd = bucket["rpd_count"] + calls
            would_be_rpm = len(bucket["minute_requests"]) + calls
            if would_be_rpd > budget.rpd or would_be_rpm > budget.rpm:
                self._save(state)
                raise GeminiQuotaExhaustedError(
                    f"{model} has no free-tier headroom for {calls} more call(s): "
                    f"{bucket['rpd_count']}/{budget.rpd} today, "
                    f"{len(bucket['minute_requests'])}/{budget.rpm} this minute."
                )

            bucket["rpd_count"] = would_be_rpd
            bucket["minute_requests"].extend([now] * calls)
            self._save(state)

    def seconds_until_headroom(self, estimated_calls: Optional[int] = None, now: Optional[float] = None) -> float:
        """How long until `reserve(estimated_calls)` would succeed, best-effort.

        0.0 means it would succeed right now. Added for MS6's promotion run:
        `promote_reviewed` previously treated `GeminiQuotaExhaustedError` as
        "stop the whole batch" even when the exhaustion was RPM-bound and
        would clear within `_RPM_WINDOW_SECONDS` on its own — this is the
        piece that lets a caller wait the actual right amount instead of
        giving up. Distinguishes the two real cases: an RPM wall clears
        within a minute (wait for the specific reservation that must age
        out); an RPD wall does not clear until the Pacific day rolls over,
        which can be hours — callers that don't want to block that long
        should compare the return value against their own ceiling rather
        than assume this is always short.

        Does not mutate state — a plain read, not a reservation.
        """
        if self._unmetered:
            return 0.0

        calls = estimated_calls if estimated_calls is not None else self._calls_per_operation
        now = now if now is not None else time.time()

        with self._lock:
            state = self._load()
            self._roll_day_if_needed(state, now)
            best: Optional[float] = None
            for model in self._chain:
                budget = self._budgets[model]
                bucket = self._model_bucket(state, model)
                self._prune_minute_window(bucket, now)

                # `calls > budget.rpm` is structurally unsatisfiable by this
                # model at ANY point via RPM alone — the sliding window can
                # never hold more than `rpm` entries, so no wait fixes it.
                # Treat it the same as RPD exhaustion (this model is out
                # until the day rolls over) rather than indexing before the
                # start of `reqs` below. Real callers never hit this: every
                # model in KNOWN_MODEL_BUDGETS has rpm >= DEFAULT_CALLS_PER_OPERATION.
                if bucket["rpd_count"] + calls > budget.rpd or calls > budget.rpm:
                    wait = _seconds_until_next_pacific_midnight(now)
                else:
                    reqs = sorted(bucket["minute_requests"])
                    if len(reqs) + calls <= budget.rpm:
                        return 0.0
                    overflow = len(reqs) + calls - budget.rpm
                    oldest_relevant = reqs[overflow - 1]
                    # `_prune_minute_window` keeps `t >= now - WINDOW`, i.e.
                    # a reservation is still counted exactly at the boundary
                    # (>=, not >). Landing exactly on `oldest_relevant + WINDOW`
                    # would therefore NOT prune it — a caller sleeping for
                    # exactly the un-padded value would find itself still
                    # blocked. The epsilon guarantees "strictly past."
                    wait = max(0.0, (oldest_relevant + _RPM_WINDOW_SECONDS) - now) + 0.01

                best = wait if best is None else min(best, wait)
            self._save(state)  # persist the day-roll/pruning even on a pure read
            return best if best is not None else 0.0

    def status(self, now: Optional[float] = None) -> dict:
        """Report current usage vs. ceiling for every model with a ledger entry.

        Reports the union of the chain and whatever the ledger has actually
        recorded, not the chain alone. The chain-only version had a blind
        spot exactly where it mattered: `gemini-embedding-001` is metered via
        `reserve_model()` and is deliberately *not* in the chain (it has no
        fallback sibling to walk to), so an operator checking status saw no
        embedding usage at all -- right up to a live 429 on a quota nothing
        was reporting.
        """
        if self._unmetered:
            return {"day": None, "unmetered": True, "models": {}}

        now = now if now is not None else time.time()
        with self._lock:
            state = self._load()
            self._roll_day_if_needed(state, now)
            report: dict = {"day": state.get("day"), "unmetered": False, "models": {}}
            tracked = list(dict.fromkeys([*self._chain, *state.get("models", {})]))
            for model in tracked:
                budget = self._budgets.get(model)
                if budget is None:
                    # A ledger entry with no budget can only come from a
                    # hand-edited state file or a removed model; surface it
                    # rather than raising KeyError on a reporting call.
                    continue
                bucket = self._model_bucket(state, model)
                self._prune_minute_window(bucket, now)
                report["models"][model] = {
                    "rpm_used": len(bucket["minute_requests"]),
                    "rpm_limit": budget.rpm,
                    "rpd_used": bucket["rpd_count"],
                    "rpd_limit": budget.rpd,
                    "in_chain": model in self._chain,
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
            # Imported here rather than at module scope purely to keep this
            # module importable on its own; server.core.config has no
            # dependency on it, so there is no cycle either way.
            from server.core.config import load_config

            config = load_config()
            hosted_model = {
                "anthropic": config.anthropic_model,
                "openai": config.openai_model,
            }.get(config.llm_provider)
            if hosted_model is not None:
                # A paid API key (MS10a): the provider enforces its own rate
                # limits, and its SDK retries 429/529 with backoff. The
                # free-tier ledger would only throttle against Gemini's
                # numbers, so it is switched off the same way as for local.
                _DEFAULT_LIMITER = GeminiRateLimiter(
                    chain=[hosted_model],
                    budgets={},
                    state_path=_default_state_path(),
                    unmetered=True,
                )
                return _DEFAULT_LIMITER
            if config.llm_is_local:
                # Local inference has no quota to enforce, so the ledger has
                # nothing to protect. It is switched off rather than given
                # unreachable ceilings: every reserve() would otherwise load,
                # mutate and re-save a JSON file, and a full backfill issues
                # thousands of them. The object still exists, and still
                # raises the same exception type, so every caller's
                # GeminiQuotaExhaustedError handling stays wired up for a
                # later switch back to Gemini.
                _DEFAULT_LIMITER = GeminiRateLimiter(
                    chain=[config.local_llm_model],
                    budgets={},
                    state_path=_default_state_path(),
                    unmetered=True,
                )
                return _DEFAULT_LIMITER

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
