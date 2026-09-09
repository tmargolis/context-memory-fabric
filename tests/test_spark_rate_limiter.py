"""Phase 4: unmetered local path, embedder metering, provider-aware delay.

The embedder cases are the important ones. `gemini-embedding-001` had a
budget defined in KNOWN_MODEL_BUDGETS but was absent from DEFAULT_MODEL_CHAIN,
so `reserve()` never debited it -- the ledger reported ample headroom right up
to a live 429 RESOURCE_EXHAUSTED on the free tier's 1,000/day embedding limit.
"""

import asyncio
from pathlib import Path

import pytest

from server.core.rate_limiter import (
    KNOWN_MODEL_BUDGETS,
    GeminiQuotaExhaustedError,
    GeminiRateLimiter,
    classify_transient_error,
    get_default_rate_limiter,
    reset_default_rate_limiter,
)
from server.providers.metered_embedder import MeteredEmbedder, maybe_meter


@pytest.fixture
def ledger(tmp_path) -> Path:
    return tmp_path / "state.json"


def _exhaust_daily(limiter: GeminiRateLimiter, model: str, start: float = 1_000_000.0) -> float:
    """Spend a model's whole daily budget without tripping its RPM ceiling.

    gemini-embedding-001 allows 1,000/day but only 100/minute, so the daily
    budget is unreachable in a single reservation. Walk forward a minute at a
    time; returns a timestamp still inside the same Pacific day.
    """
    budget = KNOWN_MODEL_BUDGETS[model]
    now = start
    spent = 0
    while spent < budget.rpd:
        chunk = min(budget.rpm, budget.rpd - spent)
        limiter.reserve_model(model, calls=chunk, now=now)
        spent += chunk
        now += 61.0
    return now


@pytest.fixture
def metered(ledger) -> GeminiRateLimiter:
    return GeminiRateLimiter(
        chain=["gemini-3.5-flash-lite"], budgets=KNOWN_MODEL_BUDGETS, state_path=ledger
    )


class _FakeEmbedder:
    """Records what it was asked for; never touches a network."""

    def __init__(self) -> None:
        self.singles = 0
        self.batches: list[int] = []
        self.config = object()

    async def create(self, input_data):
        self.singles += 1
        return [0.0] * 768

    async def create_batch(self, input_data_list):
        self.batches.append(len(input_data_list))
        return [[0.0] * 768 for _ in input_data_list]


# --- transient classification --------------------------------------------


def test_lm_studio_model_unloaded_is_transient():
    """Auto-Evict kills in-flight requests with this; a retry recovers.

    It is not an HTTP 5xx and matched none of the original markers, so it
    would otherwise abort a batch run outright.
    """
    assert classify_transient_error(Exception('{"error": "Model unloaded."}')) == "unavailable"


def test_unrelated_errors_stay_non_transient():
    assert classify_transient_error(Exception("ValidationError: field required")) is None


# --- unmetered local path -------------------------------------------------


def test_unmetered_limiter_never_refuses(ledger):
    rl = GeminiRateLimiter(
        chain=["zai-org/glm-4.7-flash"], budgets={}, state_path=ledger, unmetered=True
    )
    for _ in range(500):
        assert rl.reserve() == "zai-org/glm-4.7-flash"
    assert rl.seconds_until_headroom() == 0.0


def test_unmetered_limiter_writes_no_ledger(ledger):
    """Not merely cosmetic: a full backfill issues thousands of reservations,
    and each metered one loads, mutates and re-saves this JSON file."""
    rl = GeminiRateLimiter(
        chain=["zai-org/glm-4.7-flash"], budgets={}, state_path=ledger, unmetered=True
    )
    for _ in range(50):
        rl.reserve()
        rl.reserve_model("anything", calls=100)
    assert not ledger.exists()


def test_unmetered_skips_the_unknown_model_check(ledger):
    """A local model id is deliberately absent from KNOWN_MODEL_BUDGETS."""
    GeminiRateLimiter(
        chain=["some/model-that-google-never-heard-of"],
        budgets={},
        state_path=ledger,
        unmetered=True,
    )


def test_metered_limiter_still_rejects_unknown_models(ledger):
    with pytest.raises(ValueError, match="no known budget"):
        GeminiRateLimiter(
            chain=["some/local-model"], budgets=KNOWN_MODEL_BUDGETS, state_path=ledger
        )


def test_default_limiter_follows_the_provider_switch(monkeypatch):
    monkeypatch.setattr("server.core.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("FALKORDB_DATABASE", "test-graph")
    monkeypatch.setenv("GEMINI_API_KEY", "k")

    monkeypatch.setenv("CMF_LLM_PROVIDER", "local")
    monkeypatch.setenv("CMF_LOCAL_LLM_MODEL", "zai-org/glm-4.7-flash")
    reset_default_rate_limiter()
    local = get_default_rate_limiter()
    assert local.unmetered is True
    assert local.chain == ["zai-org/glm-4.7-flash"]

    monkeypatch.setenv("CMF_LLM_PROVIDER", "gemini")
    reset_default_rate_limiter()
    gemini = get_default_rate_limiter()
    assert gemini.unmetered is False
    assert "gemini-3.5-flash-lite" in gemini.chain
    reset_default_rate_limiter()


# --- reserve_model --------------------------------------------------------


def test_reserve_model_debits_the_named_model(metered):
    metered.reserve_model("gemini-embedding-001", calls=20)
    status = metered.status()
    assert status["models"]["gemini-embedding-001"]["rpd_used"] == 20


def test_reserve_model_refuses_past_the_daily_ceiling(metered):
    now = _exhaust_daily(metered, "gemini-embedding-001")
    with pytest.raises(GeminiQuotaExhaustedError, match="gemini-embedding-001"):
        metered.reserve_model("gemini-embedding-001", calls=1, now=now)


def test_reserve_model_refuses_past_the_minute_ceiling(metered):
    """RPM binds long before RPD here: 100/minute against 1,000/day."""
    budget = KNOWN_MODEL_BUDGETS["gemini-embedding-001"]
    metered.reserve_model("gemini-embedding-001", calls=budget.rpm, now=1_000_000.0)
    with pytest.raises(GeminiQuotaExhaustedError, match="this minute"):
        metered.reserve_model("gemini-embedding-001", calls=1, now=1_000_000.0)


def test_reserve_model_will_not_meter_an_unknown_model(metered):
    """Silently allowing an unbudgeted model is how this gap arose."""
    with pytest.raises(ValueError, match="No known budget"):
        metered.reserve_model("gemini-experimental-9000", calls=1)


def test_reserve_model_does_not_consume_the_generation_chain(metered):
    """Embedding and generation have separate ceilings and must not share."""
    _exhaust_daily(metered, "gemini-embedding-001")
    assert metered.reserve() == "gemini-3.5-flash-lite"


# --- MeteredEmbedder ------------------------------------------------------


def test_embedder_debits_one_call_per_create(metered):
    inner = _FakeEmbedder()
    embedder = MeteredEmbedder(inner, rate_limiter=metered)
    for _ in range(5):
        asyncio.run(embedder.create("text"))
    assert inner.singles == 5
    assert metered.status()["models"]["gemini-embedding-001"]["rpd_used"] == 5


def test_embedder_debits_per_input_not_per_batch(metered):
    """GeminiEmbedder forces batch_size=1 for gemini-embedding-001, so a
    batch of N is N HTTP requests and N debits. Counting it as one is exactly
    the undercount that let the quota run out unnoticed."""
    inner = _FakeEmbedder()
    embedder = MeteredEmbedder(inner, rate_limiter=metered)
    asyncio.run(embedder.create_batch(["a", "b", "c", "d"]))
    assert metered.status()["models"]["gemini-embedding-001"]["rpd_used"] == 4


def test_embedder_reserves_before_calling_so_refusal_costs_nothing(metered):
    """A caller that is refused must have made zero API requests.

    Exhausts the *minute* window rather than the day, at the real clock:
    MeteredEmbedder calls reserve_model() without an explicit `now`, so a
    ledger built at fabricated timestamps would simply be discarded by the
    day-roll when a real-time call arrives.
    """
    import time as _time

    inner = _FakeEmbedder()
    embedder = MeteredEmbedder(inner, rate_limiter=metered)
    budget = KNOWN_MODEL_BUDGETS["gemini-embedding-001"]
    metered.reserve_model("gemini-embedding-001", calls=budget.rpm, now=_time.time())

    with pytest.raises(GeminiQuotaExhaustedError):
        asyncio.run(embedder.create("text"))
    assert inner.singles == 0, "the inner embedder must not have been reached"


def test_local_embedder_is_not_wrapped():
    """nomic-embed has no quota; a metering layer that always says yes is noise."""
    inner = _FakeEmbedder()
    assert maybe_meter(inner, embed_is_local=True) is inner


def test_gemini_embedder_is_wrapped():
    inner = _FakeEmbedder()
    assert isinstance(maybe_meter(inner, embed_is_local=False), MeteredEmbedder)


# --- provider-aware pacing ------------------------------------------------


def test_inter_call_delay_follows_the_provider(monkeypatch):
    from server.consolidation.promotion import (
        GEMINI_INTER_CALL_DELAY,
        LOCAL_INTER_CALL_DELAY,
        default_inter_call_delay,
    )

    monkeypatch.setattr("server.core.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("FALKORDB_DATABASE", "g")
    monkeypatch.setenv("GEMINI_API_KEY", "k")

    monkeypatch.setenv("CMF_LLM_PROVIDER", "gemini")
    assert default_inter_call_delay() == GEMINI_INTER_CALL_DELAY == 3.5

    monkeypatch.setenv("CMF_LLM_PROVIDER", "local")
    assert default_inter_call_delay() == LOCAL_INTER_CALL_DELAY == 0.2


# --- promotions ledger is graph-scoped ------------------------------------


def _promotion_store(tmp_path, monkeypatch, graph="graph-a"):
    monkeypatch.setattr("server.core.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("FALKORDB_DATABASE", graph)
    from server.consolidation.promotion import PromotionStore

    return PromotionStore(tmp_path / "journal.db")


def test_promotion_is_scoped_to_its_graph(tmp_path, monkeypatch):
    """The whole point of widening the key.

    Without graph_name in it, a memory promoted into the Gemini graph counts
    as promoted everywhere, so rebuilding into a second graph silently skips
    every row and comes up empty with a clean-looking ledger.
    """
    store = _promotion_store(tmp_path, monkeypatch, graph="graph-a")
    store.record_success("m1", "ep1", "graph-a")

    assert store.is_promoted("m1", "graph-a") is True
    assert store.is_promoted("m1", "graph-b") is False
    store.close()


def test_the_same_memory_can_be_promoted_into_two_graphs(tmp_path, monkeypatch):
    """Makes the Gemini-vs-local A/B repeatable rather than one-shot."""
    store = _promotion_store(tmp_path, monkeypatch)
    store.record_success("m1", "ep1", "graph-a")
    store.record_success("m1", "ep1", "graph-b")
    assert store.graphs() == ["graph-a", "graph-b"]
    assert store.is_promoted("m1", "graph-a") and store.is_promoted("m1", "graph-b")
    store.close()


def test_graph_defaults_to_the_configured_target(tmp_path, monkeypatch):
    """Existing call sites pass no graph; their natural reading -- "promoted
    into the graph I am working with" -- must be the one they get."""
    store = _promotion_store(tmp_path, monkeypatch, graph="graph-a")
    store.record_success("m1", "ep1", "graph-a")
    assert store.is_promoted("m1") is True

    monkeypatch.setenv("FALKORDB_DATABASE", "graph-b")
    assert store.is_promoted("m1") is False
    store.close()


def test_failure_rows_are_also_graph_scoped(tmp_path, monkeypatch):
    """graph_name is half the key and SQLite allows NULLs in a non-INTEGER
    primary key, so a nullable failure row would insert repeatedly instead of
    updating in place."""
    store = _promotion_store(tmp_path, monkeypatch)
    store.record_failure("m1", "boom", "graph-a")
    store.record_failure("m1", "boom again", "graph-a")
    rows = store._conn.execute("SELECT * FROM promotions WHERE memory_id='m1'").fetchall()
    assert len(rows) == 1, "a repeated failure must update, not duplicate"
    assert rows[0]["error"] == "boom again"
    assert rows[0]["graph_name"] == "graph-a"
    store.close()


def test_migration_backfills_and_is_idempotent(tmp_path, monkeypatch):
    """Rows written before graph_name was recorded must land somewhere real."""
    import sqlite3

    from server.consolidation.promotion import PromotionStore, _migrate_promotions_pk

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE promotions (
            memory_id TEXT PRIMARY KEY, status TEXT NOT NULL, episode_name TEXT,
            graph_name TEXT, error TEXT, promoted_at TEXT NOT NULL
        );
        INSERT INTO promotions VALUES ('old1','succeeded','ep',NULL,NULL,'2026-01-01');
        INSERT INTO promotions VALUES ('old2','succeeded','ep','','2026-01-01','2026-01-01');
        INSERT INTO promotions VALUES ('old3','succeeded','ep','named-graph',NULL,'2026-01-01');
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr("server.core.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("FALKORDB_DATABASE", "backfilled")

    store = PromotionStore(db)
    rows = {r["memory_id"]: r["graph_name"] for r in store._conn.execute("SELECT * FROM promotions")}
    assert rows == {"old1": "backfilled", "old2": "backfilled", "old3": "named-graph"}

    pk = {c[1] for c in store._conn.execute("PRAGMA table_info(promotions)").fetchall() if c[5]}
    assert pk == {"memory_id", "graph_name"}

    _migrate_promotions_pk(store._conn)  # second run must be a no-op
    assert store._conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0] == 3
    store.close()
