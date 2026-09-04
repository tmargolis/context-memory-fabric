# Source Event 1.0 — Worked Examples

Companion to [source-event-1.0.json](source-event-1.0.json). Each example is a real shape produced by one of Milestone 2's importers, chosen to make the `observed_at` / `event_date` / `date_precision` distinction and the `event_id` identity rule concrete rather than abstract.

## 1. A ChatGPT conversation turn (native export)

A user message from a real ChatGPT `conversations-*.json` export, journaled by `server/importers/chatgpt.py`.

```json
{
  "schema_version": "1.0",
  "event_id": "chatgpt:67abc123-conv:msg-9f2e1a",
  "event_type": "turn.completed",
  "source": {
    "harness": "chatgpt",
    "conversation_id": "67abc123-conv",
    "turn_id": "msg-9f2e1a",
    "model": null
  },
  "actor_type": "user",
  "actor_id": null,
  "observed_at": "2026-04-08T12:10:07.976705Z",
  "event_date": "2026-04-08T12:10:07.976705Z",
  "date_precision": "exact",
  "content": {"text": "Got an MRI scheduled for the neck injury next week."},
  "content_hash": "sha256:...",
  "parent_event_ids": [],
  "metadata": {"sender": "user"}
}
```

**Why `observed_at` equals `event_date` here:** ChatGPT's export gives an exact message timestamp that is simultaneously "when CMF received this evidence" (during the 2026-09-01 import run) — no, wait: `observed_at` is not the import time. Read on to example 4, which is where these two genuinely diverge; here they coincide because the message's own `created_at` **is** both the best-available occurrence time and is treated as the observation anchor for an event journaled from a direct, high-fidelity export. The two fields are logically distinct even when numerically equal.

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

The 57 episodes already in `memory-fabric` were imported before the journal existed (see Milestone 2's backfill task). Their source events are reconstructed after the fact from `imports/results/*.json` and `imports/state/import_registry_memory-fabric.json`, not journaled at capture time.

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
