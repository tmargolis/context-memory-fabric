"""Provider-agnostic core: protocols, canonical models, configuration, and errors.

Introduced in Milestone 1 (see docs/adr/0002-provider-boundaries.md) to give
memory, knowledge, and event storage a stable seam that concrete backends
(Graphiti/FalkorDB, the local file corpus, a future event journal) implement
against, rather than being imported directly by server.mcp/server.context.
"""
