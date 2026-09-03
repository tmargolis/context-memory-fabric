"""Shared pytest configuration for Context Memory Fabric.

Isolates the test suite's FalkorDB writes into a dedicated graph, distinct
from the operator's production graph. Two real tests (test_step6_mcp_tools.py
and test_step6b_proposals.py) call the live `remember`/`propose` MCP tools,
which write into whatever graph `FALKORDB_DATABASE` resolves to. Prior to
this fixture, that was the same graph Claude Desktop reads, which is how
`phase1_step6_atlas_test_memory` and `step6b_orion_sqlite_decision` ended up
duplicated in the production graph. See docs/adr/0003-graph-and-state-topology.md.
"""

from pathlib import Path

from dotenv import dotenv_values
import pytest

TEST_GRAPH_NAME = "cmf_test"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"


def _configured_production_graph() -> str | None:
    """Read FALKORDB_DATABASE directly from the .env file on disk.

    Deliberately bypasses os.environ / load_dotenv(), since this module runs
    before we override FALKORDB_DATABASE below and must see the operator's
    actual configured value, not the test override.
    """
    if not _ENV_PATH.exists():
        return None
    return dotenv_values(_ENV_PATH).get("FALKORDB_DATABASE")


_prod_graph = _configured_production_graph()
if _prod_graph and _prod_graph.strip() == TEST_GRAPH_NAME:
    raise RuntimeError(
        f"Refusing to run tests: .env FALKORDB_DATABASE is set to "
        f"'{TEST_GRAPH_NAME}', which is reserved for the test suite. "
        f"Point FALKORDB_DATABASE at your production graph instead."
    )

# Set before any `server.*` module is imported by a test module, so that
# module's `load_dotenv()` call (override=False by default) does not clobber
# this. conftest.py is imported by pytest before test collection.
import os  # noqa: E402

os.environ["FALKORDB_DATABASE"] = TEST_GRAPH_NAME


@pytest.fixture(scope="session", autouse=True)
def _production_graph_untouched():
    """Assert the test session made zero writes to the production graph.

    Best-effort: skips silently if FalkorDB is unreachable or the production
    graph does not exist yet, so the suite still runs in environments without
    a live FalkorDB (e.g. CI running only pure-parser tests).
    """
    import redis

    def _snapshot() -> tuple[int, int] | None:
        if not _prod_graph or not _prod_graph.strip():
            return None
        try:
            r = redis.Redis(
                host=os.getenv("FALKORDB_HOST", "localhost"),
                port=int(os.getenv("FALKORDB_PORT", "6379")),
                password=os.getenv("FALKORDB_PASSWORD") or None,
                decode_responses=True,
                socket_connect_timeout=1,
            )
            nodes = r.execute_command("GRAPH.QUERY", _prod_graph, "MATCH (n) RETURN count(n)")[1][0][0]
            edges = r.execute_command("GRAPH.QUERY", _prod_graph, "MATCH ()-[e]->() RETURN count(e)")[1][0][0]
            return (nodes, edges)
        except Exception:
            return None

    before = _snapshot()
    yield
    if before is None:
        return
    after = _snapshot()
    assert after == before, (
        f"Test session wrote to the production graph '{_prod_graph}': "
        f"(nodes, edges) went from {before} to {after}. Tests must target "
        f"'{TEST_GRAPH_NAME}' only."
    )
