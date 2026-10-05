"""CMFFalkorDriver's edge full-text override and the FalkorDB TIMEOUT drift check."""

import asyncio

from graphiti_core.driver.driver import GraphProvider
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.search import search_utils
from graphiti_core.search.search_filters import SearchFilters

from server.providers.falkor_driver import (
    MIN_QUERY_TIMEOUT_MS,
    CMFFalkorDriver,
    FalkorSearchInterface,
    check_query_timeout,
    format_query_timeout_line,
)


class _FakeClient:
    def __init__(self, timeout=None, error=None):
        self.timeout, self.error = timeout, error

    def config_get(self, name):
        assert name == "TIMEOUT"
        if self.error:
            raise self.error
        return self.timeout


class _RecordingDriver:
    """Just enough of a FalkorDriver for Graphiti's search functions."""

    provider = GraphProvider.FALKORDB

    def __init__(self, search_interface=None):
        self.search_interface = search_interface
        self.queries: list[tuple[str, dict]] = []

    def build_fulltext_query(self, query, group_ids=None, max_query_length=128):
        return query

    async def execute_query(self, cypher, **params):
        self.queries.append((cypher, params))
        return [], None, None


def test_edge_fulltext_binds_yielded_relationship():
    driver = _RecordingDriver(FalkorSearchInterface())
    asyncio.run(search_utils.edge_fulltext_search(driver, "openclaw hardware", SearchFilters(), ["g"], 20))

    (cypher, params), = driver.queries
    assert "{uuid: rel.uuid}" not in cypher
    assert "YIELD relationship AS e, score" in cypher
    assert "startNode(e) AS n, endNode(e) AS m" in cypher
    assert "n:Entity AND m:Entity" in cypher
    assert "e.group_id IN $group_ids" in cypher
    assert params["group_ids"] == ["g"] and params["limit"] == 20


def test_edge_fulltext_keeps_node_label_filter():
    driver = _RecordingDriver(FalkorSearchInterface())
    asyncio.run(search_utils.edge_fulltext_search(driver, "q", SearchFilters(node_labels=["Person"]), None, 5))

    (cypher, _), = driver.queries
    assert "n:Person AND m:Person" in cypher


def test_other_searches_fall_back_to_graphiti_without_recursing():
    # These four don't catch NotImplementedError in Graphiti, so the
    # interface must hand them back to Graphiti's own query.
    driver = _RecordingDriver(FalkorSearchInterface())
    f = SearchFilters()
    asyncio.run(search_utils.node_fulltext_search(driver, "q", f, None, 5))
    asyncio.run(search_utils.episode_fulltext_search(driver, "q", f, None, 5))
    asyncio.run(search_utils.node_similarity_search(driver, [0.1, 0.2], f, None, 5))
    asyncio.run(search_utils.edge_similarity_search(driver, [0.1, 0.2], None, None, f, None, 5))
    assert len(driver.queries) == 4


def test_driver_keeps_search_interface_across_clone():
    driver = CMFFalkorDriver(falkor_db=object(), database="mem-fabric-local")
    assert isinstance(driver, FalkorDriver)
    assert isinstance(driver.search_interface, FalkorSearchInterface)
    for database in ("mem-fabric-local", "other-graph", driver.default_group_id):
        assert driver.clone(database).search_interface is driver.search_interface


def test_timeout_check_warns_below_minimum():
    value, warning = check_query_timeout(_FakeClient(timeout=1000))
    assert value == 1000
    assert "1000 ms" in warning and str(MIN_QUERY_TIMEOUT_MS) in warning


def test_timeout_check_accepts_minimum_and_unlimited():
    assert check_query_timeout(_FakeClient(timeout=MIN_QUERY_TIMEOUT_MS)) == (MIN_QUERY_TIMEOUT_MS, None)
    assert check_query_timeout(_FakeClient(timeout=0)) == (0, None)  # 0 = no timeout


def test_timeout_check_never_raises():
    value, warning = check_query_timeout(_FakeClient(error=ConnectionError("refused")))
    assert value is None and "refused" in warning


def test_timeout_line():
    assert format_query_timeout_line(30000, None) == "- **FalkorDB TIMEOUT:** 30000 ms"
    assert "⚠️" in format_query_timeout_line(1000, "too low")
