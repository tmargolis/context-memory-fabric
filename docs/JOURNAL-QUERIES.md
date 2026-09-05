# Journal Query Guide — Inspecting the Evidence Layer

This document is the companion to [FALKORDB-QUERIES.md](FALKORDB-QUERIES.md), but for the **evidence layer** rather than the memory graph: the append-only source-event journal introduced in Milestone 2. See [ADR 0001](adr/0001-four-layer-model.md) for why evidence (what was actually said) is kept separate from memory (what CMF derived from it).

**Where it lives:** `imports/journal/journal.db` — a single SQLite file (gitignored; it's real personal data, not source code), holding three tables: `events` (the evidence itself), `derived_memories` (Milestone 3's local classification staging — never written to FalkorDB), and `consolidation_jobs`. Schema: [docs/schemas/source-event-1.0.json](schemas/source-event-1.0.json), worked examples in [source-event-1.0-examples.md](schemas/source-event-1.0-examples.md).

There are two ways to query it: the journal CLI (`server/journal/cli.py`) for the common cases, and raw `sqlite3` for anything else.

---

## ⚡ Quick Start: The Journal CLI

Every command defaults to `imports/journal/journal.db`; pass `--db <path>` to point elsewhere (e.g. a test fixture).

```bash
# Summary counts: total, by harness, by event_type, by retention_class,
# by_provenance (captured vs. reconstructed per harness), date range
python -m server.journal.cli stats

# One event, full envelope, by its event_id
python -m server.journal.cli inspect "claude:2230d3a6-215c-47cb-9bcf-fc4e3e160523:019ac0d2-c415-75bb-a5ca-afe2682ee10d"

# Export matching events as JSONL (optionally filtered)
python -m server.journal.cli export --out /tmp/chatgpt_events.jsonl --harness chatgpt
python -m server.journal.cli export --out /tmp/one_conv.jsonl --conversation-id <conversation_id>

# Deterministic re-emission for reproducibility checks (byte-identical across runs
# against an unchanged journal — this is Milestone 2's replay acceptance test)
python -m server.journal.cli replay --out /tmp/replay.jsonl --harness gemini
```

`inspect` and `export`/`replay` all reconstruct the nested `source: {harness, conversation_id, session_id, turn_id, model}` envelope shape — see the "Storage note" at the bottom of [source-event-1.0-examples.md](schemas/source-event-1.0-examples.md) for why the underlying SQLite table stores those as flat columns instead.

---

## 🧭 Direct SQLite: `events` table

Run these with `sqlite3 imports/journal/journal.db "<query>"`, or open an interactive shell with `sqlite3 imports/journal/journal.db`.

### Schema at a glance
```sql
.schema events
```
Key columns: `event_id` (PK, the idempotency key), `harness`, `conversation_id`/`session_id`/`turn_id` (flattened `source.*`), `actor_type` (`user`/`assistant`/`tool`/`system`), `observed_at`/`event_date`/`date_precision`, `content_json`, `content_hash`, `metadata_json`, `retention_class`.

### Counts by harness and event type
```sql
SELECT harness, event_type, COUNT(*) n, MIN(event_date) earliest, MAX(event_date) latest
FROM events GROUP BY harness, event_type ORDER BY harness, n DESC;
```

### Actor split (who's "speaking" in each harness)
```sql
SELECT harness, actor_type, COUNT(*) n FROM events GROUP BY harness, actor_type ORDER BY harness;
```

### Read a specific event's content and metadata
```sql
SELECT event_id, observed_at, event_date, date_precision,
       content_json, metadata_json
FROM events WHERE event_id = 'chatgpt:69c92f60-5fe4-8328-9249-4ad4f3c332f2:1953954a-0478-4033-9cfc-a8d43f5d50d2';
```
`content_json`/`metadata_json`/`parent_event_ids_json`/`attachment_refs_json`/`privacy_json` are all stored as serialized JSON text — use SQLite's `json_extract()` to filter or project into them directly, e.g.:
```sql
SELECT event_id, json_extract(content_json, '$.text') AS text
FROM events WHERE harness = 'gemini' AND actor_type = 'user' LIMIT 5;
```

### Distinguish live-captured evidence from reconstructed/backfilled evidence
```sql
SELECT json_extract(metadata_json, '$.provenance_reconstructed') AS reconstructed, COUNT(*) n
FROM events WHERE harness = 'chatgpt' GROUP BY reconstructed;
```
`metadata.provenance_reconstructed = true` marks an event whose `observed_at` is a backfill-run artifact, not a genuine capture timestamp (see example 4 in the schema examples doc) — never present it to a user as "CMF learned this on `observed_at`."

### Walk a conversation's turns in order, with parent linkage
```sql
SELECT event_id, actor_type, observed_at, json_extract(content_json, '$.text') AS text, parent_event_ids_json
FROM events WHERE conversation_id = '<conversation_id>' ORDER BY observed_at;
```

### Full-text-ish search across journaled content
SQLite has no FTS index on this table by design (evidence retrieval at scale is a later-milestone concern, not MS2's job) — a `LIKE` scan works fine at the current ~19k-row size:
```sql
SELECT event_id, harness, event_date, json_extract(content_json, '$.text') AS text
FROM events WHERE content_json LIKE '%some phrase%' ORDER BY event_date DESC LIMIT 20;
```

---

## 🧭 Direct SQLite: `derived_memories` and `consolidation_jobs`

These hold Milestone 3's local classification of every journaled event — never written to FalkorDB (see "Will journal entries end up in FalkorDB?" below).

### Category/approval breakdown across the whole journal
```sql
SELECT category, approval_state, COUNT(*) n FROM derived_memories GROUP BY category, approval_state ORDER BY category;
```

### The classification for one event, alongside the event itself
```sql
SELECT e.event_id, e.harness, json_extract(e.content_json,'$.text') AS text,
       d.category, d.approval_state, d.confidence, d.reason
FROM events e JOIN derived_memories d ON d.source_event_id = e.event_id
WHERE e.event_id = '<event_id>';
```

### Sanity check: every event has exactly one derivation, nothing orphaned
```sql
-- Should equal COUNT(*) FROM events
SELECT COUNT(*) FROM derived_memories;
-- Should be zero
SELECT COUNT(*) FROM derived_memories dm LEFT JOIN events e ON dm.source_event_id = e.event_id WHERE e.event_id IS NULL;
```

### The auto-accept candidates (the smallest, highest-confidence slice)
```sql
SELECT source_event_id, statement, confidence, event_date
FROM derived_memories WHERE approval_state = 'auto_accepted' ORDER BY confidence DESC;
```

---

## Will journal entries end up in FalkorDB?

**Not directly, and not as-is.** Per [ADR 0001](adr/0001-four-layer-model.md), the journal (evidence) and Graphiti/FalkorDB (memory) are deliberately separate stores with no code path connecting them today — `imports/journal/journal.db` and FalkorDB's graphs are written by entirely different code, and zero Graphiti writes have been made from any journal-derived data as of Milestone 3.

The intended future path is narrower than "the journal flows into the graph": only rows in `derived_memories` with `category = 'episodic'` and `approval_state = 'auto_accepted'` would ever become candidates for promotion into Graphiti as new episodes, and each promotion would still go through `remember()`/`reconcile_memories()` like any other episodic write, becoming `Episodic`/`Entity` nodes in the existing graph shape — not a new node type "children of Entities." Raw evidence stays in the journal, queryable here, and most of it never reaches FalkorDB at all. So the ~19k-row scale concern doesn't really apply to the graph — it's filtered down by orders of magnitude before anything is eligible for promotion, and promotion itself is not yet turned on.

> **Stale figures / criteria (ADR 0005, 2026-09-05).** The "264 of 19,107, about 1.4%" count and the "must be a dated decision to auto-accept" criterion predate two changes: the classifier tightening that took `auto_accepted` to 7 (see IMPLEMENTATION-PLAN.md's promotion milestone), and ADR 0005, which adds model-derived **reasoning episodes** (`episodic` rows carrying a `reasoning_kind` property) and loosens the auto-accept gate for them. The eligibility rule — `category = 'episodic'` AND `approval_state = 'auto_accepted'`, promoted via `remember()` — is unchanged; what qualifies and how many is not. The MS4a privacy/cost gate is answered, not "still-open"; MS3.5 (the next milestone) runs reasoning extraction on the same rate-limited path.
