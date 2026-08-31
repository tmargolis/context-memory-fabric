# Context Memory Fabric

A portable context and memory layer for AI clients, combining durable knowledge with episodic memory and exposing them through the Model Context Protocol (MCP) for shared cross-model context.

## Goal
- **Durable knowledge:** `tmargolis/LLM_Wiki` on GitHub
- **Episodic/current memory:** Graphiti + FalkorDB
- **Access layer:** a custom MCP service that can query both

Phase 1 should prove that a memory saved in one session can be recalled later, while Wiki knowledge remains separately identifiable and authoritative.

## Target Architecture

```text
                Context Memory Fabric MCP
                         |
             +-----------+-----------+
             |                       |
       LLM_Wiki                   Graphiti
   durable knowledge          episodic memory
        GitHub                   FalkorDB
             |                       |
             +-----------+-----------+
                         |
                    get_context()
```

## Key Principles

- **Separation of Concerns:**
  - **LLM_Wiki (Canonical Knowledge):** The Wiki remains canonical and authoritative for durable, structured, reference knowledge.
  - **Graphiti (Episodic Memory):** Stores recent decisions, events, evolving preferences, active project states, and entity relationships.
- **Explicit Aggregation:** The MCP layer (`get_context()`) retrieves and presents both context sources side-by-side without silently merging conflicts.
- **Portability:** Any MCP-compliant client (IDE agents, chat interfaces, CLI tools) can access and update shared context seamlessly.
