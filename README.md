# Context Memory Fabric

**Status:** Early prototype / Phase 1

A portable context and memory substrate shared across AI clients, combining durable knowledge with episodic memory and exposing them through the Model Context Protocol (MCP) for unified context assembly.

## Purpose

Context Memory Fabric provides a portable context and memory layer that AI clients (IDE agents, chat interfaces, and CLI tools) can use to maintain continuity across sessions, projects, and platforms while preserving clear provenance.

## Architectural Distinction

- **LLM_Wiki (`tmargolis/LLM_Wiki`):** Curated / durable knowledge. Acts as the canonical, authoritative reference source for documentation, patterns, specifications, and structured reference material.
- **Graphiti + FalkorDB:** Episodic / current memory. Maintains dynamic knowledge graphs of recent sessions, temporal facts, decisions, evolving preferences, active project state, and entity relationships.
- **MCP Layer:** Unified access and context assembly. Exposes standard Model Context Protocol tools and resources (`get_context()`, etc.) to query both durable knowledge and episodic memory without silently merging conflicts.

```text
                Context Memory Fabric MCP
                         |
             +-----------+-----------+
             |                       |
        LLM_Wiki                  Graphiti
    durable knowledge         episodic memory
      (GitHub Repo)              (FalkorDB)
             |                       |
             +-----------+-----------+
                         |
                    get_context()
```

## Phase 1 Scope

Phase 1 is strictly a proof of the memory-vs-durable-knowledge architecture. It verifies that:
1. Episodic memories saved in one session can be reliably recalled in later sessions.
2. Durable knowledge from the Wiki remains separately identifiable and authoritative.
3. The MCP service can assemble and serve both context streams side-by-side with clear provenance.
