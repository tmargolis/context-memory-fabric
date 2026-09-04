# Source Event 1.0 — Worked Examples

Companion to [source-event-1.0.json](source-event-1.0.json). Each example is a real shape produced by one of Milestone 2's importers, chosen to make the `observed_at` / `event_date` / `date_precision` distinction and the `event_id` identity rule concrete rather than abstract.

## 1. A ChatGPT conversation turn (native export)

An assistant message from Todd's real ChatGPT `conversations-*.json` export (`imports/source/chatgpt-export/full_export-2026-09-01/`), journaled by `server/importers/chatgpt.py` on 2026-09-04 — 577 conversations, 6,343 events. This is a genuine journaled row, not an illustrative shape; content is otherwise redacted-by-selection for a low-sensitivity example.

```json
{
  "schema_version": "1.0",
  "event_id": "chatgpt:69c92f60-5fe4-8328-9249-4ad4f3c332f2:1953954a-0478-4033-9cfc-a8d43f5d50d2",
  "event_type": "turn.completed",
  "source": {
    "harness": "chatgpt",
    "conversation_id": "69c92f60-5fe4-8328-9249-4ad4f3c332f2",
    "turn_id": "1953954a-0478-4033-9cfc-a8d43f5d50d2",
    "model": null
  },
  "actor_type": "assistant",
  "actor_id": null,
  "observed_at": "2026-03-29T13:55:48.143670Z",
  "event_date": "2026-03-29T13:55:48.143670Z",
  "date_precision": "exact",
  "content": {"text": "Hello—good to see you. How can I help today?", "role": "assistant"},
  "content_hash": "sha256:...",
  "parent_event_ids": [],
  "metadata": {"conversation_title": "Hello response"}
}
```

**Why `observed_at` equals `event_date` here:** the message's own `create_time` from the export is both the best-available occurrence time and the anchor `journal_chatgpt_export` uses for `observed_at` — for a direct, high-fidelity export (as opposed to a backfilled/reconstructed one), the two are the same real timestamp read twice, not two different measurements that happen to agree. Example 4 is where they genuinely diverge, because there `observed_at` is a backfill-run artifact rather than a captured timestamp at all.

## 2. A Claude conversation turn, mid-branch

Claude's export encodes a full message tree via `parent_message_uuid`. `server/importers/claude.py` reconstructs the active path (see its module docstring for the branch-selection heuristic) and preserves the parent pointer as `parent_event_ids`, so a branch point remains visible in the journal even though only the active path was walked.

```json
{
  "schema_version": "1.0",
  "event_id": "claude:c8ee1ee5-c0fe-4899-be14-c628776d0134:019e233e-7453-7d7c-ab8e-d68f4aa03fa4",
  "event_type": "turn.completed",
  "source": {
    "harness": "claude",
    "conversation_id": "c8ee1ee5-c0fe-4899-be14-c628776d0134",
    "turn_id": "019e233e-7453-7d7c-ab8e-d68f4aa03fa4",
    "model": null
  },
  "actor_type": "assistant",
  "observed_at": "2026-05-13T21:29:16.758997Z",
  "event_date": "2026-05-13T21:29:16.758997Z",
  "date_precision": "exact",
  "content": {
    "text": "...",
    "blocks": [{"type": "text", "text": "..."}]
  },
  "content_hash": "sha256:...",
  "parent_event_ids": ["claude:c8ee1ee5-c0fe-4899-be14-c628776d0134:019e233e-7453-7fc7-8798-765115bc6aa7"],
  "metadata": {"sender": "assistant"}
}
```

`actor_type: "assistant"` matters downstream (Milestone 3): this event alone can never establish a personal fact about the user during consolidation, only corroborate or contextualize one.

## 3. A Claude project snapshot — the mutable-entity case

Unlike a conversation turn, a Claude project can be edited after creation. `event_id` therefore incorporates the snapshot's `updated_at`, not just the project's stable `uuid` — otherwise re-importing the same project after it changed would be silently treated as a duplicate of its earlier state, rather than a new, distinct snapshot event.

```json
{
  "schema_version": "1.0",
  "event_id": "claude:project:019bd85c-e5d2-70a3-8c0d-1ee6dad695e1:2026-01-19T22:25:25.459604+00:00",
  "event_type": "project.snapshot",
  "source": {"harness": "claude", "conversation_id": null, "turn_id": null},
  "actor_type": "user",
  "observed_at": "2026-01-19T22:25:25.459604Z",
  "event_date": "2026-01-19T22:25:25.459604Z",
  "date_precision": "exact",
  "content": {
    "name": "3D Horizons",
    "description": "create stereo pairs of artworks for custom view master reel",
    "docs": []
  },
  "content_hash": "sha256:...",
  "metadata": {"is_starter_project": false}
}
```

## 4. A backfilled event — where `observed_at` and `event_date` genuinely diverge

57 episodes already in `memory-fabric` were imported before the journal existed (see Milestone 2's backfill task) and were reconstructed after the fact from `imports/results/*.json` and `imports/state/import_registry_memory-fabric.json`, not journaled at capture time. (A further 18 were separately recovered from a pre-deletion `default_db` graph snapshot — see IMPLEMENTATION-PLAN.md's Milestone 2 notes; both sets carry `metadata.provenance_reconstructed: true`, distinguishing them from the 6,343 native turns journaled directly from the export in example 1.)

```json
{
  "schema_version": "1.0",
  "event_id": "chatgpt:backfill:cand_8f7ed87e0b4a",
  "event_type": "turn.completed",
  "source": {"harness": "chatgpt", "conversation_id": null, "turn_id": null},
  "actor_type": "user",
  "observed_at": "2026-09-03T00:00:00Z",
  "event_date": "2014-01-01T00:00:00Z",
  "date_precision": "year",
  "content": {"text": "..."},
  "content_hash": "sha256:...",
  "metadata": {
    "provenance_reconstructed": true,
    "backfilled_from": "imports/state/import_registry_memory-fabric.json",
    "candidate_id": "cand_8f7ed87e0b4a"
  }
}
```

Here `observed_at` (2026-09-03, the day of the backfill run) is nowhere near `event_date` (2014-01-01, a retrospective reference to a 2014 gesture presentation — see `tests/test_regressions_baseline.py`). `metadata.provenance_reconstructed: true` is the honest marker that this event's `observed_at` is a backfill artifact, not a genuine capture timestamp — do not present it to a user as "CMF learned this on 2026-09-03."

## 5. A markdown-summary import — no native ID, `event_id` falls back to content hash

The original (pre-journal) `import_memories` tool has no per-item identifier at all — just parsed text spans from a pasted summary. `event_id` therefore derives entirely from `content_hash`.

```json
{
  "schema_version": "1.0",
  "event_id": "chatgpt:sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
  "event_type": "memory_summary.section",
  "source": {"harness": "chatgpt", "conversation_id": null, "turn_id": null},
  "actor_type": "user",
  "observed_at": "2026-09-01T17:24:05Z",
  "event_date": "2025-08-01T00:00:00Z",
  "date_precision": "exact",
  "content": {"text": "On 2025-08-01, Todd believes his June 2, 2025 EV-charging request became deemed approved by operation of law.", "section_heading": "EV Charging Approvals"},
  "content_hash": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
  "metadata": {"import_id": "imp_20260901_212105_chatgpt"}
}
```

## Summary: how to read `observed_at` vs. `event_date`

| Question | Field |
|---|---|
| When did CMF learn/capture this evidence? | `observed_at` |
| When did the thing described actually happen in the world? | `event_date` (nullable — null means unknown, never "now") |
| How precisely is `event_date` known? | `date_precision` |
| Is this event a live capture or a later reconstruction? | `metadata.provenance_reconstructed` (absent/false = live) |

## Storage note: `source` is flattened in SQLite, not dropped

Every example above shows `source` as a nested object because that's the wire/schema shape (`SourceProvenance` in `server/core/models.py`, and the `source` object in `source-event-1.0.json`). `server/journal/store.py`'s `events` table has no nested-object column type, so `SqliteEventStore` stores `source.harness` / `.account_scope` / `.conversation_id` / `.session_id` / `.turn_id` / `.model` as six top-level columns (indexed individually — that's what makes querying by harness or conversation cheap) and reconstructs the nested `SourceProvenance` object on every read (`_row_to_event`). The journal CLI's `inspect`/`export`/`replay` commands all emit the nested form shown here, never the flattened column layout — so nothing about `source` is actually lossy or inconsistent between the schema and the code, but if you query `journal.db` directly with `sqlite3`, expect flat `harness`/`conversation_id`/... columns rather than a `source` blob.
