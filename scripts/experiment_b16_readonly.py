"""Process-local guards for B16 retrieval; never change the installed driver."""

from contextlib import contextmanager
import re


@contextmanager
def readonly_graphiti():
    """Suppress automatic index writes and enforce Redis read-only commands.

    Even a query accidentally containing a mutation is rejected by FalkorDB.
    Install before constructing any Graphiti driver; restore on exit.
    """
    from falkordb.asyncio.graph import AsyncGraph
    from graphiti_core.driver.falkordb_driver import FalkorDriver

    original_query = AsyncGraph.query
    original_indices = FalkorDriver.build_indices_and_constraints

    async def read(self, q, params=None, timeout=None):
        return await self.ro_query(q, params=params, timeout=timeout or 30000)

    async def no_indices(self, *args, **kwargs):
        return None

    AsyncGraph.query = read
    FalkorDriver.build_indices_and_constraints = no_indices
    try:
        yield
    finally:
        AsyncGraph.query = original_query
        FalkorDriver.build_indices_and_constraints = original_indices


@contextmanager
def scratch_graphiti(graph_name):
    """Permit writes only to the exact B16 scratch, suppress implicit index setup."""
    if not re.fullmatch(r"mem-fabric-b16-merge-\d{8}", graph_name):
        raise ValueError("Expected an explicit B16 scratch graph")
    from falkordb.asyncio.graph import AsyncGraph
    from graphiti_core.driver.falkordb_driver import FalkorDriver

    original_query = AsyncGraph.query
    original_indices = FalkorDriver.build_indices_and_constraints

    async def query(self, q, params=None, timeout=None):
        if self.name != graph_name:
            # Cloned/default drivers may read another graph but cannot write it.
            return await self.ro_query(q, params=params, timeout=timeout or 30000)
        return await original_query(self, q, params=params, timeout=timeout or 30000)

    async def no_indices(self, *args, **kwargs):
        return None

    AsyncGraph.query = query
    FalkorDriver.build_indices_and_constraints = no_indices
    try:
        yield
    finally:
        AsyncGraph.query = original_query
        FalkorDriver.build_indices_and_constraints = original_indices
