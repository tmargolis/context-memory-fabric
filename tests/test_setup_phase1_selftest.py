"""Setup simplification Phase 1, item 1.7: the self-test
(docs/setup-simplification-plan.md). Graphiti and FalkorDB are faked."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server import selftest
from server.core import status as server_status
from server.core.startup_check import CheckResult


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CMF_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("FALKORDB_DATABASE", "CMF-local")
    monkeypatch.setenv("CMF_LLM_PROVIDER", "local")
    monkeypatch.setenv("CMF_EMBED_PROVIDER", "local")
    monkeypatch.setenv("EMBEDDING_DIM", "768")


class FakeGraphiti:
    def __init__(self, graph, width=768, finds=True, write_error=None):
        self.graph, self.width, self.finds, self.write_error = graph, width, finds, write_error
        self.body = ""
        self.closed = False
        self.embedder = SimpleNamespace(create=AsyncMock(side_effect=lambda input_data: [0.0] * self.width))

    async def add_episode(self, **kw):
        if self.write_error:
            raise self.write_error
        self.body = kw["episode_body"]

    async def search(self, query, num_results=10):
        if not self.finds:
            return [SimpleNamespace(fact="something else")]
        marker = next(w for w in self.body.split() if w.startswith("selftest"))
        return [SimpleNamespace(fact=f"Robin Selftest prefers {marker} tea")]

    async def close(self):
        self.closed = True


def _run(problems=(), **graphiti):
    made = []

    def factory(graph):
        made.append(FakeGraphiti(graph, **graphiti))
        return made[-1]

    deleted = []
    with patch("server.core.startup_check.check", return_value=CheckResult(problems=list(problems))), \
         patch.object(selftest, "delete_scratch_graph", side_effect=deleted.append):
        result = asyncio.run(selftest.run_selftest(factory=factory))
    return result, made, deleted


def test_passes_and_cleans_up():
    result, made, deleted = _run()
    assert result["ok"], result
    assert [s["stage"] for s in result["stages"]] == ["config", "embed", "write", "recall", "cleanup"]
    assert result["graph"].startswith("cmf_selftest_") and made[0].graph == result["graph"]
    assert deleted == [result["graph"]] and made[0].closed
    assert json.loads(selftest.result_path().read_text())["ok"] is True


def test_config_problem_stops_before_any_provider_call():
    result, made, deleted = _run(problems=["The LLM provider is openai, but OPENAI_API_KEY isn't set."])
    assert not result["ok"] and made == []
    assert result["stages"][0] == {"stage": "config", "ok": False, "seconds": 0.0,
                                   "detail": "The LLM provider is openai, but OPENAI_API_KEY isn't set."}
    assert result["stages"][-1]["stage"] == "cleanup"


def test_width_mismatch_fails_embed_stage():
    result, _, deleted = _run(width=1024)
    assert [s["stage"] for s in result["stages"]] == ["config", "embed", "cleanup"]
    assert "1024-wide vectors, EMBEDDING_DIM is 768" in result["stages"][1]["detail"]
    assert deleted  # cleanup still runs


def test_write_failure_is_classified():
    import redis

    result, _, _ = _run(write_error=redis.exceptions.ConnectionError("Error 61 connecting to localhost:6379."))
    write = next(s for s in result["stages"] if s["stage"] == "write")
    assert not write["ok"] and "localhost:6379" in write["detail"]


def test_recall_miss_fails():
    result, _, _ = _run(finds=False)
    recall = next(s for s in result["stages"] if s["stage"] == "recall")
    assert not result["ok"] and "search didn't return it" in recall["detail"]


def test_timeout_still_cleans_up():
    async def slow(**kw):
        await asyncio.sleep(5)

    def factory(graph):
        g = FakeGraphiti(graph)
        g.add_episode = slow
        return g

    deleted = []
    with patch("server.core.startup_check.check", return_value=CheckResult()), \
         patch.object(selftest, "delete_scratch_graph", side_effect=deleted.append):
        result = asyncio.run(selftest.run_selftest(timeout=0.2, factory=factory))
    assert [s["stage"] for s in result["stages"]][-2:] == ["timeout", "cleanup"] and deleted


def test_cleanup_refuses_other_graphs():
    with pytest.raises(ValueError):
        selftest.delete_scratch_graph("mem-fabric-local")


def test_cleanup_deletes_only_an_existing_scratch_graph():
    client = MagicMock()
    client.execute_command.side_effect = lambda *a: [b"cmf_selftest_ab12", b"mem-fabric-local"] if a == ("GRAPH.LIST",) else "OK"
    with patch("server.core.falkordb_conn.redis_client", return_value=client):
        selftest.delete_scratch_graph("cmf_selftest_ab12")
        selftest.delete_scratch_graph("cmf_selftest_gone")
    deletes = [c.args for c in client.execute_command.call_args_list if c.args[0] == "GRAPH.DELETE"]
    assert deletes == [("GRAPH.DELETE", "cmf_selftest_ab12")]


def test_health_carries_the_latest_result():
    _run(finds=False)
    with patch.object(server_status, "falkordb_status", return_value={"reachable": False, "graph_exists": None}):
        snap = server_status.snapshot()
    assert snap["selftest"]["ok"] is False and snap["selftest"]["failed_stage"] == "recall"


def test_format_result():
    result, _, _ = _run()
    text = selftest.format_result(result)
    assert text.startswith("CMF self-test passed in") and "[ok] recall" in text
