"""Backward-compatible re-export shim.

The Graphiti/FalkorDB episodic memory implementation moved to
server.providers.memory_graphiti as part of Milestone 1 (see
docs/adr/0002-provider-boundaries.md). This module re-exports the same
names so existing imports (server.context, server.importer,
server.chatgpt_export_parser's lazy import) and
unittest.mock.patch("server.memory.X") call sites in the test suite keep
working unchanged. New code should import from
server.providers.memory_graphiti directly, or depend on the
server.core.protocols.MemoryProvider protocol instead of this module.
"""

from server.core.rate_limiter import GeminiQuotaExhaustedError
from server.providers.memory_graphiti import (
    GraphitiMemoryProvider,
    MissingGraphConfigurationError,
    close_graphiti,
    create_graphiti,
    edit_memory,
    format_edit_memory_results_for_mcp,
    format_memory_results_for_mcp,
    format_reconcile_results_for_mcp,
    get_graphiti,
    get_graphiti_for_operation,
    parse_iso_datetime,
    reconcile_memories,
    recall,
    remember,
    resolve_target_database,
)

__all__ = [
    "GeminiQuotaExhaustedError",
    "GraphitiMemoryProvider",
    "MissingGraphConfigurationError",
    "close_graphiti",
    "create_graphiti",
    "edit_memory",
    "format_edit_memory_results_for_mcp",
    "format_memory_results_for_mcp",
    "format_reconcile_results_for_mcp",
    "get_graphiti",
    "get_graphiti_for_operation",
    "parse_iso_datetime",
    "reconcile_memories",
    "recall",
    "remember",
    "resolve_target_database",
]
