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
MATCH (n:Entity {name: 'User'})-[r]-(other)
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

## H2. Project Grouping (CMF-local — not written by Graphiti)

> [!NOTE]
> Added to `mem-fabric-local` as a post-promotion pass, now `scripts/tag_projects.py` (originally 2026-09-09; re-run and re-labeled 2026-09-12 after the FalkorDB persistence incident — see `cmf-falkordb-persistence-incident` memory — wiped and required rebuilding this graph). Two episode-level handles for `derived_memories.project`, plus a `Project` hub node wired to **entities**, not episodes. Graphiti never reads any of this — `MATCH (e:Episodic …)` / `MATCH (n:Entity …)` are unaffected, and `build_communities()` only ever matches `RELATES_TO`, so `IN_PROJECT` is invisible to it too. A fresh re-promotion, or a ledger rebuild (`scripts/rebuild_graph_from_ledger.py`), recreates Episodic/Entity nodes *without* any of this, so `scripts/tag_projects.py --graph mem-fabric-local` must be re-run after either.
>
> - **`e.project`** — plain string property on every `:Episodic` node, the raw project id (e.g. `career-navigator`). For code / precise filters.
> - **project label** — second label on each episode (`['Episodic','job_hunt']`), sanitized (dashes → underscores; `career-navigator` → `job_hunt` is the one display-name override, see `DISPLAY_OVERRIDES` in the script). No `ep_` prefix as of 2026-09-12 (dropped — the prefix bought nothing once the label itself is already a project name and never collides with `:Entity`/`:Episodic`). Gives per-project **colour** in the FalkorDB Browser graph view. Does **not** give single-click hide — the Browser shows a node while *any* of its labels is on, and `:Episodic` must stay.
> - **`(:Project {name})` + `[:IN_PROJECT]`** — one hub node per project, wired primarily to `:Entity` (never `:Episodic` directly gets one *unless* it has to — see below). Always `:Project`, never `:Entity`. Changed 2026-09-12 from episode-level to entity-level per user requirement — entities are the more useful clustering target for the force-view, and unlike episodes an entity can genuinely belong to more than one project (e.g. "Mac Pro" links to `openclaw`, `mac_infra`, `personal_admin`, *and* `misc`), so this is many-to-many by construction: one `IN_PROJECT` edge per distinct project among an entity's mentioning episodes, derived from `MENTIONS` + `e.project`, not the ledger. **~25% of episodes extract zero entities** (no `MENTIONS` edges at all), which left them totally disconnected from the hub the first time this shipped — `scripts/tag_projects.py` now gives those a direct `Episodic-[:IN_PROJECT]->Project` edge as a fallback, so every project-tagged episode reaches its hub one way or the other, never both.

### All Episodes in One Project (property — exact)
```cypher
MATCH (e:Episodic {project: 'condo'})
RETURN e.name AS name, e.valid_at AS valid_at, e.content AS content
ORDER BY e.valid_at ASC
```

### Isolate One Project + Its Neighbourhood (label — for the Browser)
```cypher
MATCH (e:condo)
OPTIONAL MATCH (e)-[r]-(m)
RETURN *
```

### Everything EXCEPT One Project
```cypher
MATCH (e:Episodic)
WHERE NOT 'condo' IN labels(e)
OPTIONAL MATCH (e)-[r]-(m)
RETURN *
LIMIT 500
```

### Expand a Project Hub Node
```cypher
// Both link kinds at once: entities in the project (with their mentioning
// episodes) plus entity-less episodes linked to it directly.
MATCH (p:Project {name: 'condo'})<-[:IN_PROJECT]-(x)
OPTIONAL MATCH (x)<-[:MENTIONS]-(e:Episodic)  // only matches when x is an Entity
RETURN p, x, e
```

### Entity Count per Project
```cypher
MATCH (p:Project)<-[:IN_PROJECT]-(n:Entity)
RETURN p.name AS project, count(n) AS entities
ORDER BY entities DESC
```

### Episode Count per Project (property, not the hub)
```cypher
MATCH (e:Episodic)
WHERE e.project IS NOT NULL
RETURN e.project AS project, count(e) AS episodes
ORDER BY episodes DESC
```

### Entities Belonging to More Than One Project
```cypher
MATCH (n:Entity)-[:IN_PROJECT]->(p:Project)
WITH n, collect(p.name) AS projects
WHERE size(projects) > 1
RETURN n.name AS entity, projects
ORDER BY size(projects) DESC
```

### Entities Shared Across Two Projects (via the hub)
```cypher
MATCH (a:Project {name: 'openclaw'})<-[:IN_PROJECT]-(n:Entity)-[:IN_PROJECT]->(b:Project {name: 'obsidian'})
RETURN DISTINCT n.name AS shared_entity
ORDER BY shared_entity
```

### Entities Shared Across Two Projects (via episode mentions — equivalent, no hub needed)
```cypher
MATCH (a:Episodic {project: 'openclaw'})-[:MENTIONS]->(n:Entity)<-[:MENTIONS]-(b:Episodic {project: 'obsidian'})
RETURN DISTINCT n.name AS shared_entity
ORDER BY shared_entity
```

### List Every Project Label With Its Count
```cypher
MATCH (e:Episodic)
UNWIND labels(e) AS l
WITH l WHERE l <> 'Episodic'
RETURN l AS project_label, count(*) AS episodes
ORDER BY episodes DESC
```

### Undo the Project-Grouping Pass
```cypher
// property
MATCH (e:Episodic) REMOVE e.project;
// labels — repeat per ep_* label, or script it
MATCH (e:ep_condo) REMOVE e:ep_condo;
// hub nodes + edges
MATCH (p:Project) DETACH DELETE p
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
  - **CMF-local additions** (`mem-fabric-local` only, post-promotion pass — see §H2): `project` (String); a second label `ep_<name>` per episode.
- **`:Entity`**: Named entity or concept extracted across episodes.
  - Properties: `uuid` (String), `name` (String), `summary` (String), `group_id` (String).
- **`:Community`**: Cluster of closely related entities (hierarchical grouping).
- **`:Saga`**: High-level multi-episode narrative arc.
- **`:Project`** *(CMF-local, not Graphiti)*: one hub node per `derived_memories.project`. Property: `name` (String). Linked to episodes only.

### Relationship Types
- **`[:MENTIONS]`**: Directed edge from `(:Episodic)` to `(:Entity)`.
- **`[:RELATES_TO]`**: Directed edge between `(:Entity)` and `(:Entity)` capturing a temporal fact (`fact`, `valid_at`, `invalid_at`, `episodes`).
- **`[:HAS_MEMBER]`**: Community to Entity membership edge.
- **`[:HAS_EPISODE]`**: Saga to Episodic relationship edge.
- **`[:NEXT_EPISODE]`**: Chronological transition between episodes.
- **`[:IN_PROJECT]`** *(CMF-local, not Graphiti)*: `(:Episodic)` → `(:Project)`.

### Graph naming (2026-09-09)
- **`mem-fabric-local`** — live graph. qwen3.5-122b extraction, `nomic-embed` (768-dim). Episodes named `<harness>-<project>-NNN`.
- **`mem-fabric-gemini`** — pre-migration, 1024-dim, retained untouched (rollback).
- **`mem-fabric-local-glm`** — rejected Phase 7 GLM build, kept as the A/B record.
