# Nightly Review of Captured Memory

CMF's background pollers stage new **episodes** (EP) and **doc proposals** (DOC) all day. Nothing reaches your memory or wiki until it's reviewed. The nightly review does the first pass for you: a scheduled task in the AI app of your choice reviews the day's items at 11:11 PM, recommends a verdict for each, and leaves you a summary to confirm in the morning.

It only **recommends**. Nothing changes until you say yes. One yes does everything: it records the verdicts, writes the approved docs into the wiki and promotes the approved episodes into memory. There is no separate promote or apply step and no dry run; the summary is what you approve.

## What you need

- **CMF connected to that app** as an MCP connector (docs/CLIENTS.md). A scheduled task usually runs in the provider's cloud, not on your computer, so the app must reach CMF over the internet: the streamable-HTTP server with OAuth behind a tunnel or VPN (docs/CLIENTS.md, "Running the Server Standalone"). An app that runs on your own machine can use the local server.
- **A plan with scheduled tasks** in that app (notes below).
- **Optional: your own review rules.** CMF's generic rules are in `server/review/review_rules.md`. Put your own in a Markdown file outside the repo and point `CMF_REVIEW_RULES_PATH` at it in `.env`, for example which notes belong in which wiki folder, or which topics are always episodes and never pages. They are appended to the generic rules on every run.

## Set it up: copy this into your AI app

Paste the block below into a new chat in the app that will run the review. It asks the app to create the scheduled task itself.

````text
Create a scheduled task named "CMF nightly review" that runs every day at 11:11 PM my local time and uses my Context Memory Fabric (CMF) connector. Each run should do exactly this:

1. Call get_review_batch. If its "items" list is empty, reply "CMF nightly review: nothing new to review." and stop.
2. Read its "rules" field first, and follow those rules for every judgment.
3. Judge every item in "items" (EP = a staged episode, DOC = a doc proposal) as approve, reject or flag, each with a short reason a person can check in one read. Use get_doc_proposal to read a DOC's diff when you need it.
4. For a DOC with "trips_overwrite_check": true, compare it with the live page. If the page already covers it, reject it as covered. Otherwise draft an additive rebuild with propose_doc_update (the full current page with only the missing facts added) and recommend approve with "rebuild_proposal_id" set to the new proposal's id.
5. Call record_review_recommendations with the run_id and one entry per item: label, verdict, reason, and optionally a shorter summary and a rebuild_proposal_id.
6. End by showing me the summary that record_review_recommendations returns, unchanged.

During the scheduled run, never call confirm_review_recommendations, review_episode, bulk_review_episodes, review_doc_proposal, apply_doc_proposal, promote_approved_episodes, remember or capture_session. Those wait for me.
````

## In the morning

Open the task's latest run in that app. The summary has two tables, **Episodes (EP)** and **Doc proposals (DOC)**, with flagged items first, each with the recommended verdict and the reason. Then reply in that chat:

- **"Yes."** Accepts every recommendation and makes it live.
- **"Yes, but EP3 approve, DOC2 reject, skip EP7."** Changes a few first; a flagged item needs a verdict from you or it stays in the queue.

What a yes does (`confirm_review_recommendations`):

- Approved docs are written to the wiki right away and committed in its git repo. A doc whose page changed since the proposal was made, or that would remove more than 30% of its page, is not written; the reply names it and it stays approved for you to look at.
- Approved episodes are promoted by a background process on the machine running CMF, one at a time (1–4 minutes each on Spark), each waiting its turn for the Spark slot so it doesn't collide with the pollers. It keeps going after the chat or app closes. The reply gives its log, `imports/review/promote_<run_id>.log`. An episode that fails stays approved; `python -m server.review.cli promote --apply` retries everything approved but not yet promoted.

From any other chat or app, `list_review_recommendations` shows the latest unconfirmed run again, and `confirm_review_recommendations` confirms it. Items a run has recommended on aren't offered to the next run; a night with nothing new reports so. Each run covers at most 50 items, oldest first; a backlog is worked through over several nights.

## Notes per app

Scheduled-task features change often. Check your app's own help for the current steps; these notes are as of October 2026.

- **Claude (Cowork):** scheduled tasks run remotely, even with your computer asleep, and can use your connectors. Create it from the Scheduled page, or by pasting the block above into a Cowork chat. Each run is its own session, listed under Scheduled. See Anthropic's [Schedule recurring tasks in Claude Cowork](https://support.claude.com/en/articles/13854387).
- **ChatGPT:** scheduled tasks can use connected apps (formerly connectors) and run at most hourly. Your CMF server is a custom MCP app, added in Developer Mode. Sources disagree on whether Plus and Pro accounts can use a custom app's *write* tools, and the review needs one (`record_review_recommendations`); if the task can only read, use another app for this.
- **Gemini Spark:** "schedules" run recurring tasks, and since June 2026 Spark connects to custom MCP servers by URL. Rolling out to Google AI Pro (US) and Ultra subscribers; see [What's new for Gemini Spark](https://support.google.com/gemini/answer/17171264).
- **Antigravity:** the 2.0 desktop app adds scheduled tasks (`/schedule`). It runs on your computer, so it can use the local CMF server; the computer has to be awake at 11:11 PM.
- **Claude Code:** a desktop scheduled task or a cloud routine works too. A cloud routine needs the remote CMF connector; a local one needs the machine awake.
