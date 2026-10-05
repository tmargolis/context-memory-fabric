"""CMF's FalkorDB driver: a faster edge full-text search and a TIMEOUT drift check.

Two fixes for the same symptom — "Query timed out" from broad `recall_mem`
queries and from `add_episode`'s dedup search (docs/plan-active.md, Backlog,
"FalkorDB `TIMEOUT` drift and a slow Graphiti full-text join"):

- **The slowness.** Graphiti's `edge_fulltext_search` joins each full-text
  hit back with `MATCH (n:Entity)-[e:RELATES_TO {uuid: rel.uuid}]->(m:Entity)`,
  which FalkorDB plans as a label scan of every `Entity` per hit. A broad
  question (MS7 case A1, 352 hits) took 10.9 s on `mem-fabric-local`; binding
  the yielded relationship and reading its endpoints with `startNode`/
  `endNode` takes 0.03 s. Over all 30 MS7 eval questions: 212.7 s -> 0.56 s,
  with identical rows in identical order (2026-09-29).
- **The drift.** docker-compose sets `TIMEOUT 30000`, but a container created
  before that change, or a live `GRAPH.CONFIG SET` lost to a restart, leaves
  the server at 1000. `check_query_timeout` makes that visible in the log and
  in `capture_health` instead of as failed recalls.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.driver.search_interface.search_interface import SearchInterface
from graphiti_core.edges import EntityEdge, get_entity_edge_from_record
from graphiti_core.graph_queries import get_relationships_query
from graphiti_core.models.edges.edge_db_queries import get_entity_edge_return_query
from graphiti_core.search import search_utils
from graphiti_core.search.search_filters import edge_search_filter_query_constructor

logger = logging.getLogger(__name__)

# docker-compose.yml's FALKORDB_ARGS value; below this, broad queries time out.
MIN_QUERY_TIMEOUT_MS = 30000


def falkordb_connection_params() -> dict[str, Any]:
    return {
        "host": os.getenv("FALKORDB_HOST", "localhost"),
        "port": int(os.getenv("FALKORDB_PORT", "6379")),
        "password": os.getenv("FALKORDB_PASSWORD") or None,
    }


class _DefaultSearchDriver:
    """The wrapped driver, minus its search_interface.

    Graphiti's search functions route to `driver.search_interface` whenever
    one is set, and four of them (edge/node similarity, node/episode
    full-text) don't fall back on NotImplementedError. Passing this proxy
    lets FalkorSearchInterface hand those back to Graphiti's own code
    without recursing into itself.
    """

    search_interface = None

    def __init__(self, driver: Any):
        self._driver = driver

    def __getattr__(self, name: str) -> Any:
        return getattr(self._driver, name)


class FalkorSearchInterface(SearchInterface):
    """Overrides only `edge_fulltext_search`; everything else is Graphiti's own.

    Methods not defined here inherit SearchInterface's NotImplementedError,
    which Graphiti catches and falls back from; the four it doesn't catch
    delegate explicitly below.
    """

    async def edge_fulltext_search(
        self,
        driver: Any,
        query: str,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[EntityEdge]:
        # Graphiti 0.29.3's FalkorDB branch, with the per-hit uuid re-match
        # replaced by binding the yielded relationship. The full-text index is
        # on RELATES_TO, so `e` already has the right type; n and m stay bound
        # for the node-label filters and the return clause.
        fuzzy_query = search_utils.fulltext_query(query, group_ids, driver)
        if fuzzy_query == "":
            return []

        filter_queries, filter_params = edge_search_filter_query_constructor(search_filter, driver.provider)
        filter_queries.insert(0, "n:Entity AND m:Entity")
        if group_ids is not None:
            filter_queries.append("e.group_id IN $group_ids")
            filter_params["group_ids"] = group_ids

        cypher = (
            get_relationships_query("edge_name_and_fact", limit=limit, provider=driver.provider)
            + """
            YIELD relationship AS e, score
            WITH e, score, startNode(e) AS n, endNode(e) AS m
            WHERE """
            + " AND ".join(filter_queries)
            + """
            WITH e, score, n, m
            RETURN
            """
            + get_entity_edge_return_query(driver.provider)
            + """
            ORDER BY score DESC
            LIMIT $limit
            """
        )
        records, _, _ = await driver.execute_query(
            cypher, query=fuzzy_query, limit=limit, routing_="r", **filter_params
        )
        return [get_entity_edge_from_record(record, driver.provider) for record in records]

    async def edge_similarity_search(
        self,
        driver: Any,
        search_vector: list[float],
        source_node_uuid: str | None,
        target_node_uuid: str | None,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
        min_score: float = 0.7,
    ) -> list[Any]:
        return await search_utils.edge_similarity_search(
            _DefaultSearchDriver(driver),
            search_vector,
            source_node_uuid,
            target_node_uuid,
            search_filter,
            group_ids,
            limit,
            min_score,
        )

    async def node_fulltext_search(
        self,
        driver: Any,
        query: str,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        return await search_utils.node_fulltext_search(
            _DefaultSearchDriver(driver), query, search_filter, group_ids, limit
        )

    async def node_similarity_search(
        self,
        driver: Any,
        search_vector: list[float],
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
        min_score: float = 0.7,
    ) -> list[Any]:
        return await search_utils.node_similarity_search(
            _DefaultSearchDriver(driver), search_vector, search_filter, group_ids, limit, min_score
        )

    async def episode_fulltext_search(
        self,
        driver: Any,
        query: str,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        return await search_utils.episode_fulltext_search(
            _DefaultSearchDriver(driver), query, search_filter, group_ids, limit
        )


class CMFFalkorDriver(FalkorDriver):
    """FalkorDriver with FalkorSearchInterface attached, kept across clone().

    Graphiti clones the driver per call whenever a single group_id differs
    from the driver's database (graphiti_core.decorators), and FalkorDriver's
    own clone() builds a plain FalkorDriver — which would silently drop the
    override and bring the slow join back.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.search_interface = FalkorSearchInterface()

    def clone(self, database: str) -> FalkorDriver:
        cloned = super().clone(database)
        cloned.search_interface = self.search_interface
        return cloned



def check_query_timeout(client: Optional[Any] = None) -> tuple[Optional[int], Optional[str]]:
    """Read the server's `GRAPH.CONFIG GET TIMEOUT`; return (ms, warning or None).

    Never raises: an unreachable server is reported as a warning, since this
    runs at startup and inside `capture_health`.
    """
    try:
        if client is None:
            from falkordb import FalkorDB

            client = FalkorDB(**falkordb_connection_params())
        value = int(client.config_get("TIMEOUT"))
    except Exception as exc:  # noqa: BLE001 — diagnostics must not fail their caller
        return None, f"could not read FalkorDB TIMEOUT: {exc}"
    if 0 < value < MIN_QUERY_TIMEOUT_MS:
        return value, (
            f"FalkorDB TIMEOUT is {value} ms, below the {MIN_QUERY_TIMEOUT_MS} ms docker-compose sets; "
            "broad recall_mem queries and add_episode dedup searches may time out. The container was "
            "probably created before that setting, or a live GRAPH.CONFIG SET was lost to a restart: "
            "recreate it (docs/plan-active.md, Backlog) or run "
            f"`GRAPH.CONFIG SET TIMEOUT {MIN_QUERY_TIMEOUT_MS}` as a stopgap."
        )
    return value, None


def log_query_timeout_check(client: Optional[Any] = None) -> Optional[str]:
    value, warning = check_query_timeout(client)
    if warning:
        logger.warning(warning)
    else:
        logger.info(f"FalkorDB TIMEOUT: {value} ms")
    return warning


def format_query_timeout_line(value: Optional[int], warning: Optional[str]) -> str:
    if warning:
        return f"- **FalkorDB TIMEOUT:** ⚠️ {warning}"
    return f"- **FalkorDB TIMEOUT:** {value} ms"
