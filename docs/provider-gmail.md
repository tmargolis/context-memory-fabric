# Gmail capture source (MS11b)

**Status:** Draft for User's review, 2026-10-27. No code yet.
**Milestone:** [MS11b](plan-active.md#ms11b-gmail-redesigned). Replaces the MS5 sent-mail snapshot provider and drops the planned incremental Gmail sync.
**Direction:** Gmail → CMF only. Pulling CMF context into Gmail is a possible later step, out of scope here.

## Summary

Gmail becomes a capture source like the AI assistants, not a knowledge base. When you send an email, Gmail notifies CMF within seconds; CMF fetches that thread, filters out what is not yours to keep, writes the messages to the journal, and the existing `ExtractPolicy` turns threads about your projects, work, research, writing, plans, decisions and preferences into episodes and doc proposals in the normal review queue. Once promoted they are recallable from every connected assistant and IDE. Nothing reaches the graph without review.

The trigger is Gmail's server-side push (`users.watch` → Cloud Pub/Sub), because Gmail has no "after send" hook and its MCP server has no events. CMF calls neither `remember` nor `capture_session` directly: `remember` skips review, and `capture_session` expects a model to have already judged what is worth keeping. Feeding the journal reuses the judgment, project tagging, review and promotion every other source already has.

## What Gmail offers

| Mechanism | What it does | Fit |
|---|---|---|
| Gmail MCP server (Google, developer preview; `gmailmcp.googleapis.com/mcp/v1`) | Tools for an AI client: `search_threads`, `get_thread`, labels, drafts. OAuth with `gmail.readonly` + `gmail.compose`. | Pull only: an assistant must call it. No watch, webhook or subscription. Needs the Workspace Developer Preview Program. Useful later for the CMF → Gmail direction, not as a trigger. |
| Claude's Gmail connector | The same kind of pull tools inside Claude. | Same limit. Usable for a pilot (option A below). |
| Gmail API `users.watch` + Pub/Sub | Gmail publishes a message to a Pub/Sub topic when the mailbox changes, filterable by label (e.g. `SENT`). The payload carries only `emailAddress` and `historyId`; `history.list` from the last stored `historyId` returns what changed. Watch must be renewed at least every 7 days (Google recommends daily); at most one notification per second per user. | The event source. Works for consumer and Workspace accounts. |
| Apps Script | Time-driven triggers only; Gmail add-on compose triggers fire while composing, not after sending. | No on-send event; would still poll. Rejected. |
| IMAP IDLE on Sent Mail | A held-open connection the server pings on change. | Real push with no Cloud project, but a long-lived connection per folder and an app password or XOAUTH2. Kept as the fallback if Pub/Sub setup proves too heavy for other users (D2). |

## Design

### Flow

```text
you send → Gmail → Pub/Sub topic → pull subscription → cmf-poller
  → history.list(since historyId) → fetch thread → filter → journal (harness gmail)
  → quiet period → ExtractPolicy over the thread → review queue → promote → recall
```

### Components (`server/adapters/gmail/`)

- **auth.py:** installed-app OAuth, scope `gmail.readonly` only, `access_type=offline`. Refresh token in `~/.cmf/state/gmail/token.json`, owner-only. CMF never sends, labels or deletes mail.
- **watch.py:** `users.watch` on `labelIds: ["SENT"]` (D3), renewed daily; `stop` on disable. Stores `historyId` and expiry.
- **listener.py:** Pub/Sub *pull* subscription drained by the poller container, so CMF needs no public endpoint and keeps running behind localhost. Pub/Sub keeps undelivered messages up to 7 days, so CMF downtime loses nothing. If no notification arrives for a set time, one `history.list` call catches anything dropped.
- **reader.py:** for each new sent message, fetch the full thread (`threads.get`). Parse participants, date, subject and body; strip quoted history, signatures and attachments (attachment names kept as metadata). One `SourceEvent` per message, deduped by Gmail message id, with `thread_id` as the conversation key and a Gmail web link as provenance.
- **filters.py:** decides what enters the journal (below).
- **worker.py:** registered as source `gmail` in `capture_runner`. Waits for a thread to go quiet (D4, default 30 minutes since the last message) before extracting, so a fast back-and-forth is judged once, not per reply. A later reply re-opens the thread and adds to it, using the existing thread-merge budget.

### Choosing what to keep

Layered, cheapest first. Each layer can only remove.

1. **Your participation.** Only threads where you sent at least one message. Received-only mail (newsletters, notifications, cold outreach) never enters. Inbound messages in a thread you replied to are included, since the other side's words are often the decision.
2. **Your labels.** Label a thread `CMF` to include it regardless; `CMF-skip` to exclude it. This is the "conversations you choose" control.
3. **Exclusions.** Gmail categories Promotions, Social, Updates and Forums; no-reply senders; a configurable denylist of recipients, domains and labels. Suggested defaults for User to confirm: legal counsel, medical providers, financial institutions (D5).
4. **Extraction judgment.** `ExtractPolicy` with an email prompt section: keep reasoning about User's projects, work, research, writing, plans, decisions and preferences; skip logistics, pleasantries and scheduling. Project tags come from the existing project inference.
5. **Review.** Unchanged human gate, with episodes grouped by thread in `list_review_conversations`.

### Privacy

- Email carries other people's words. They enter the journal only for threads User took part in, and reach the graph only after review.
- Extraction runs on whatever LLM the install uses. On this deployment that is local qwen on Spark, so mail text stays on User's hardware. For other users on Anthropic, OpenAI or Gemini, the app must say plainly that email text goes to that provider.
- Promoted episodes are visible to every connected client, including ChatGPT and Gemini. That is the purpose, and review is where to stop anything that should not travel. The old "should Gmail be in `get_context`" decision no longer applies: raw mail never reaches `get_context`, only reviewed episodes.
- The journal keeps stripped bodies, not raw MIME. Disabling the source stops the watch and revokes the token; a separate `cmf gmail purge` removes its journal rows.

## Decisions for User

- **D1. Account type and OAuth publishing.** `gmail.readonly` is a restricted scope. A consumer Google Cloud app left in *Testing* issues refresh tokens that expire after 7 days. Options: publish it unverified for personal use (Google shows an "unverified app" warning; to confirm), or use a Workspace account with an Internal app. Verification for wide distribution needs a security assessment and is out of scope.
- **D2. Pub/Sub or IMAP IDLE** as the event source. Recommended: Pub/Sub for this deployment; revisit for other users after Phase 3.
- **D3. Trigger label.** `SENT` only (recommended), or also `INBOX` for threads labeled `CMF` that User never replied to.
- **D4. Quiet period** before extraction (default 30 minutes).
- **D5. Default denylist** contents.
- **D6. Backfill.** Optional one-time import of the last N days of sent threads (`messages.list q="in:sent after:…"`), offered once like `cmf backfill`. Not a recurring sync.

## Phases

### Phase 0: prove the trigger (no CMF code)

- Cloud project, Gmail API and Pub/Sub enabled, topic with `gmail-api-push@system.gserviceaccount.com` as publisher, pull subscription.
- OAuth on User's account per D1; `watch` on `SENT`; send a test email and see the notification and the `history.list` result.
- Optional pilot (option A): a scheduled Claude task uses the Gmail connector to read the day's sent threads and stage them with `capture_session`, to measure the keep rate before building. Cheap, but costs tokens, runs only inside Claude, and email content reaches an agent that can call tools, so it is a measurement, not the design.

### Phase 1: adapter

- `auth`, `watch`, `listener`, `reader`, `filters` with fixture threads (quoted replies, signatures, forwards, attachments, categories, labels).
- Journal events under harness `gmail`; dedupe and replay-safe.

### Phase 2: extraction quality

- Email section in `ExtractPolicy`; quiet-period worker in `capture_runner`.
- Run on two weeks of User's sent mail into a scratch review queue. Measure keep rate, noise and project tagging; tune filters before going live.

### Phase 3: live

- Listener in `cmf-poller`; daily watch renewal; fallback history check.
- `capture_health` and Status rows: token valid, watch expiry, last notification, last thread captured, filter counts.
- Management app: Gmail moves from "coming soon" to a Sources card (connect, labels, denylist, provider privacy note, disable and purge).

### Phase 4: retire the snapshot

- Remove `server/providers/gmail/` (snapshot provider) and `scripts/import_gmail_mbox.py`, or keep them developer-only, after the listener has run a week.
- D6 backfill if wanted.

## Acceptance tests

1. A sent email about a CMF project appears in the journal within 2 minutes, and after the quiet period an episode is in review, tagged to the project, with a link back to the thread.
2. Once promoted, it is returned by `recall_mem` from Codex and from ChatGPT.
3. A newsletter, a received-only thread, a `CMF-skip` thread and a denylisted recipient produce zero journal events.
4. A five-reply thread within the quiet period produces one extraction pass, not five.
5. CMF stopped for 2 hours loses no sent mail.
6. A revoked token or lapsed watch turns its Status row red with a Fix.

## Exit gate

Do emails about User's work reach review within the hour and become recallable from other assistants, with no unreviewed mail in the graph and no surprise about who sees it?

## Later: CMF → Gmail

Not designed yet. The likely shape is the assistant doing the joining: Claude (or another client) holds both the Gmail connector or Google's Gmail MCP server and CMF, calls `get_context` while drafting, and writes a draft. A Gmail add-on sidebar calling CMF directly is the alternative. Either reuses this plan's account and consent work.

## Sources

- [Gmail MCP server configuration](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server)
- [Workspace MCP server developer preview announcement (2026-05)](https://workspaceupdates.googleblog.com/2026/05/agent-tools-and-security-updates-for-workspace-developers.html)
- [Gmail API push notifications](https://developers.google.com/workspace/gmail/api/guides/push)
- [users.watch reference](https://developers.google.com/gmail/api/v1/reference/users/watch)
