# Instructions for Your AI Assistants

Once CMF is connected to an AI app, tell that app how to use it. Paste the block below into the app's custom instructions, project instructions or system prompt (in a coding tool, its instructions file, such as `CLAUDE.md` or `AGENTS.md`). Edit the parts in [brackets] to match your setup, and delete lines for tools you don't use.

Some tools only appear when the matching feature is configured: the wiki tools (`search_wiki`, `propose_doc_update`) need `LLM_WIKI_PATH`, and `search_knowledge` covers whichever knowledge providers you enabled.

````text
Use my Context Memory Fabric (CMF) connector as my default personal context and memory layer whenever it's available. For anything involving my projects, work, research, writing, plans, decisions, preferences or prior activity, consult CMF first, before web search, my other connected apps, or general background knowledge.

Tool routing:
- get_context: the default when both durable knowledge and recent memory may matter, especially for ongoing projects and my current thinking.
- recall_mem: recent decisions, changes, past events, experiments and project state.
- search_wiki: my durable notes and source material [describe your notes, e.g. "my Obsidian vault of project notes and research"].
- search_knowledge: search every knowledge source CMF has, each result labelled with its source.
- remember: save what I explicitly ask you to remember, plus clearly important decisions, preferences, milestones, state changes or results likely to matter later. Don't save routine conversation, transient drafts, casual remarks or speculation.
- capture_session: whenever a task reaches a decision, a conclusion or a milestone, and before your final answer on any substantial task, capture the distinct things worth keeping in one call. Don't wait to be asked; if nothing qualifies, skip it. Route each item either to an episode (something that happened, was decided or was concluded) or to a doc proposal (durable, reusable knowledge that's still true read later, out of context). Prefer this to several separate remember calls.
- propose_doc_update: propose adding stable, reusable knowledge to my notes. A proposal doesn't change my notes; I review it first.

Everything you save is staged for my review; nothing reaches my memory or notes until I approve it.

After CMF, use my other connected sources [for example: email, calendar, documents, chat] for what CMF won't have: live documents, actual message threads, calendar context, and anything not yet captured. They complement CMF rather than replace it: CMF first for synthesized and curated knowledge, the others for primary source material and current communications.

If you also keep your own memory or notes about me (a local memory file, project memory), and they ever disagree with CMF, CMF is authoritative: treat CMF's version as correct and update your copy to match.

Reviews: when I ask to review what was captured, use list_review_conversations, then list_episode_proposals and list_doc_proposals. When I confirm a nightly review ("confirm", "confirm but EP3 approve"), use confirm_review_recommendations. Only promote episodes (promote_approved_episodes) or apply docs (apply_doc_proposal) when I ask.
````

## Notes

- **Name the connector as your app shows it.** If your app lists CMF under another name, use that name in the first line.
- **Keep it short in tools with small instruction limits.** The routing list is the essential part; the review paragraph can go if you review in only one place.
- **Instructions alone rarely get an agent to save anything.** Agents follow "look things up in CMF" reliably, but seldom save on their own. In Claude Code, Codex and Antigravity, add the capture-checkpoint hook, which prompts the agent every few turns (docs/SETUP.md, Background Capture); the transcript pollers are the other option there. In chat apps, these instructions and a nightly review are what you have.
- **Nightly review** has its own copy-paste block: docs/NIGHTLY-REVIEW.md.
