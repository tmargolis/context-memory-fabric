# FalkorDB & Cypher Query Reference for Context Memory Fabric

This document provides a concise, task-oriented reference of verified Cypher queries for inspecting, navigating, auditing, and validating the Graphiti / FalkorDB knowledge graph backing Context Memory Fabric (CMF).

---

## ⚡ Quick Start: Running Queries

### Running via Docker Shell (Command Line)
```bash
docker exec -it context-memory-fabric-falkordb redis-cli GRAPH.QUERY default_db   "MATCH (n) RETURN count(n) AS total_nodes"
```

### FalkorDB Web Browser UI
If running the FalkorDB web client, open:
- **URL:** `http://localhost:3000` (or configured port)
- **Graph Name:** `default_db`

---

## 🧭 Global Graph Exploration

### See the Full Graph (All Nodes & Edges)
> [!NOTE]
> For visualization tools (like FalkorDB Browser), this returns the complete graph structure. Limit results if working with large graphs.
```cypher
MATCH (s)-[r]->(t)
RETURN s, r, t
LIMIT 300
```

### Subgraph Around a Specific Concept / Entity (Neighborhood Search)
To focus on a single concept or keyword (e.g., `'Hex'`, `'Qlik'`, `'PostgreSQL'`) and see all connected nodes, edges, child facts, and mentioned episodes:
```cypher
MATCH (root)
WHERE toLower(root.name) CONTAINS 'hex' 
   OR toLower(root.content) CONTAINS 'hex' 
   OR toLower(root.summary) CONTAINS 'hex'
OPTIONAL MATCH (root)-[r1]-(neighbor)
RETURN root, r1, neighbor
LIMIT 100
```

---

## A. Graph Inventory

### Total Node Count
```cypher
MATCH (n)
RETURN count(n) AS total_nodes
```

### Node Counts by Label
```cypher
MATCH (n)
RETURN labels(n)[0] AS label, count(n) AS count
ORDER BY count DESC
```

### Total Relationship Count
```cypher
MATCH ()-[r]->()
RETURN count(r) AS total_relationships
```

### Relationship Counts by Type
```cypher
MATCH ()-[r]->()
RETURN type(r) AS relationship_type, count(r) AS count
ORDER BY count DESC
```

### List All Available Database Labels
```cypher
CALL db.labels()
```

### List All Relationship Types
```cypher
CALL db.relationshipTypes()
```

---

## B. Episodes (`:Episodic`)

### List All Episodes (Chronological by `valid_at`)
```cypher
MATCH (e:Episodic)
RETURN e.name AS name, e.valid_at AS valid_at, e.content AS content
ORDER BY e.valid_at ASC
```

### 10 Most Recent Episodes
```cypher
MATCH (e:Episodic)
RETURN e.name AS name, e.valid_at AS valid_at, e.content AS content
ORDER BY e.valid_at DESC
LIMIT 10
```

### 10 Oldest Episodes
```cypher
MATCH (e:Episodic)
RETURN e.name AS name, e.valid_at AS valid_at, e.content AS content
ORDER BY e.valid_at ASC
LIMIT 10
```

### Search Episodes by Narrative Content
```cypher
MATCH (e:Episodic)
WHERE toLower(e.content) CONTAINS 'redis'
RETURN e.name AS name, e.valid_at AS valid_at, e.content AS content
ORDER BY e.valid_at DESC
```

### Search Episodes by Identifier / Slug Name
```cypher
MATCH (e:Episodic)
WHERE toLower(e.name) CONTAINS 'reconciled'
RETURN e.name AS name, e.valid_at AS valid_at, e.content AS content
ORDER BY e.valid_at DESC
```

### Total Count of Episodes
```cypher
MATCH (e:Episodic)
RETURN count(e) AS episode_count
```

### Audit: Episodes with Zero Outgoing `MENTIONS`
```cypher
MATCH (e:Episodic)
WHERE NOT (e)-[:MENTIONS]->(:Entity)
RETURN e.uuid AS uuid, e.name AS name, e.valid_at AS valid_at, e.content AS content
ORDER BY e.valid_at ASC
```

### Show Every Entity Mentioned by Each Episode
```cypher
MATCH (e:Episodic)
OPTIONAL MATCH (e)-[:MENTIONS]->(n:Entity)
RETURN e.name AS episode_name, e.valid_at AS valid_at, collect(n.name) AS mentioned_entities
ORDER BY e.valid_at ASC
```

### Find All Episodes Mentioning a Specific Entity
```cypher
MATCH (e:Episodic)-[:MENTIONS]->(n:Entity)
WHERE toLower(n.name) CONTAINS 'postgresql'
RETURN e.name AS episode_name, e.valid_at AS valid_at, e.content AS content
ORDER BY e.valid_at ASC
```

---

## C. Entities (`:Entity`)

### List All Entity Nodes
```cypher
MATCH (n:Entity)
RETURN n.name AS name, n.summary AS summary
ORDER BY n.name ASC
```

### Search Entities by Name
```cypher
MATCH (n:Entity)
WHERE toLower(n.name) CONTAINS 'lake'
RETURN n.name AS name, n.summary AS summary
```

### Inspect Entity Summaries
```cypher
MATCH (n:Entity)
WHERE n.summary IS NOT NULL AND size(n.summary) > 0
RETURN n.name AS name, n.summary AS summary
ORDER BY n.name ASC
```

### Find Potential Duplicate Entity Names (Exact Case-Insensitive Matches)
```cypher
MATCH (n:Entity)
WITH toLower(n.name) AS normalized_name, collect(n.name) AS original_names, count(n) AS count
WHERE count > 1
RETURN normalized_name, original_names, count
```

### Show All Inbound & Outbound Relationships Connected to an Entity
```cypher
MATCH (n:Entity {name: 'Todd'})-[r]-(other)
RETURN type(r) AS relationship_type, labels(other)[0] AS other_label, other.name AS other_name, r.fact AS fact
```

---

## D. Facts / `RELATES_TO`

> [!IMPORTANT]
> The canonical relationship type in Graphiti is `RELATES_TO` (never `RELATION`).

### List All Factual Edges
```cypher
MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity)
RETURN s.name AS source, r.fact AS fact, t.name AS target, r.valid_at AS valid_at, r.invalid_at AS invalid_at, r.episodes AS episodes
ORDER BY r.valid_at ASC
```

### Active Facts Only (`invalid_at IS NULL`)
```cypher
MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity)
WHERE r.invalid_at IS NULL
RETURN s.name AS source, r.fact AS fact, t.name AS target, r.valid_at AS valid_at
ORDER BY r.valid_at ASC
```

### Invalidated / Superseded Facts Only
```cypher
MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity)
WHERE r.invalid_at IS NOT NULL
RETURN s.name AS source, r.fact AS fact, t.name AS target, r.valid_at AS valid_at, r.invalid_at AS invalid_at
ORDER BY r.invalid_at DESC
```

### Facts Involving a Particular Entity
```cypher
MATCH (s:Entity)-[r:RELATES_TO]-(t:Entity)
WHERE toLower(s.name) CONTAINS 'atlas' OR toLower(t.name) CONTAINS 'atlas'
RETURN s.name AS entity_1, r.fact AS fact, t.name AS entity_2, r.valid_at AS valid_at
```

### Search Facts by Keyword in Fact Statement
```cypher
MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity)
WHERE toLower(r.fact) CONTAINS 'database'
RETURN s.name AS source, r.fact AS fact, t.name AS target, r.valid_at AS valid_at
```

---

## E. `MENTIONS` & Graph Topology

### Entity Degree / Mention Count (Most Referenced Entities)
```cypher
MATCH (e:Episodic)-[:MENTIONS]->(n:Entity)
RETURN n.name AS entity_name, count(e) AS mention_count
ORDER BY mention_count DESC
LIMIT 20
```

### Entity Count per Episode
```cypher
MATCH (e:Episodic)
OPTIONAL MATCH (e)-[:MENTIONS]->(n:Entity)
RETURN e.name AS episode_name, count(n) AS entity_count
ORDER BY entity_count DESC
```

### Episodes that Share the Same Entity
```cypher
MATCH (e1:Episodic)-[:MENTIONS]->(n:Entity)<-[:MENTIONS]-(e2:Episodic)
WHERE id(e1) < id(e2)
RETURN n.name AS shared_entity, e1.name AS episode_1, e2.name AS episode_2
ORDER BY shared_entity ASC
```

---

## F. Temporal & Contradiction Checks

### Active Facts Chronologically Ordered
```cypher
MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity)
WHERE r.invalid_at IS NULL
RETURN r.valid_at AS valid_at, s.name AS source, r.fact AS fact, t.name AS target
ORDER BY r.valid_at ASC
```

### Heuristic Contradiction Check: Multiple Active Facts Between Same Entity Pair
> [!NOTE]
> Simple Cypher checks identify multiple co-existing facts between the same entities for human review. Semantic contradiction detection is handled by LLM evaluation.
```cypher
MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity)
WHERE r.invalid_at IS NULL
WITH s, t, collect(r.fact) AS active_facts, count(r) AS fact_count
WHERE fact_count > 1
RETURN s.name AS source, t.name AS target, active_facts, fact_count
```

---

## G. Duplicate & Data Quality Checks

### Check for Duplicate Episode Names
```cypher
MATCH (e:Episodic)
WITH e.name AS name, collect(e.uuid) AS uuids, count(e) AS count
WHERE count > 1
RETURN name, uuids, count
```

### Check for Exact Duplicate Episode Content
```cypher
MATCH (e:Episodic)
WITH trim(e.content) AS content, collect(e.name) AS episode_names, count(e) AS count
WHERE count > 1
RETURN content, episode_names, count
```

### Orphan Entity Check (No Incoming `MENTIONS` and No `RELATES_TO` Edges)
```cypher
MATCH (n:Entity)
WHERE NOT ()-[:MENTIONS]->(n) AND NOT (n)-[:RELATES_TO]-()
RETURN n.uuid AS uuid, n.name AS name, n.summary AS summary
```

---

## H. Targeted CMF Examples (Syntax Templates)

### Search Context Memory Fabric Episodes
```cypher
MATCH (e:Episodic)
WHERE toLower(e.content) CONTAINS 'context memory fabric'
RETURN e.name AS name, e.valid_at AS valid_at, e.content AS content
```

### Find Decisions / Migrations in 2026
```cypher
MATCH (e:Episodic)
WHERE e.valid_at STARTS WITH '2026'
RETURN e.name AS name, e.valid_at AS valid_at, e.content AS content
ORDER BY e.valid_at ASC
```

---

## ⚠️ I. Safe vs. Destructive Operations

All queries in Sections A through H are **READ-ONLY** and safe to execute.

### ⛔ DESTRUCTIVE: Full Graph Reset (Detaches & Deletes All Graphiti Nodes & Edges)
> [!CAUTION]
> **IRREVERSIBLE OPERATION.** This purges all Episodic, Entity, Community, and Saga nodes and all relationships. Always ensure a pre-reset backup exists before running.
```cypher
MATCH (n) DETACH DELETE n
```

### List Graphs in FalkorDB (Redis CLI)
```bash
docker exec -it context-memory-fabric-falkordb redis-cli GRAPH.LIST
```

---

## 📐 J. Graphiti Schema Specification

Context Memory Fabric utilizes the Graphiti graph schema inside FalkorDB:

### Node Labels
- **`:Episodic`**: Discrete chronological event or decision.
  - Properties: `uuid` (String), `name` (String), `valid_at` (ISO Datetime string), `content` (String), `source_description` (String), `group_id` (String).
- **`:Entity`**: Named entity or concept extracted across episodes.
  - Properties: `uuid` (String), `name` (String), `summary` (String), `group_id` (String).
- **`:Community`**: Cluster of closely related entities (hierarchical grouping).
- **`:Saga`**: High-level multi-episode narrative arc.

### Relationship Types
- **`[:MENTIONS]`**: Directed edge from `(:Episodic)` to `(:Entity)`.
- **`[:RELATES_TO]`**: Directed edge between `(:Entity)` and `(:Entity)` capturing a temporal fact (`fact`, `valid_at`, `invalid_at`, `episodes`).
- **`[:HAS_MEMBER]`**: Community to Entity membership edge.
- **`[:HAS_EPISODE]`**: Saga to Episodic relationship edge.
- **`[:NEXT_EPISODE]`**: Chronological transition between episodes.
