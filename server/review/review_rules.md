# CMF review rules

How to judge staged episodes (EP) and doc proposals (DOC). You only recommend: nothing changes until the user confirms.

## Verdicts

- **approve**: worth keeping in long-term memory or the wiki as written.
- **reject**: not worth keeping, or wrong.
- **flag**: you aren't sure, or it's the user's call. Say exactly what they need to decide.

Every recommendation needs a short reason a person can check in one read ("process narration", "duplicate of EP4", "decision with its rationale, dated").

## Episodes

Approve:
- Decisions, plans, findings, rejected alternatives and retrospectives that someone could act on later, with enough context to stand alone (what was decided, why, when).
- Dated facts about the user's work, projects, preferences or state that a later question could need.

Reject:
- **Process narration:** "the assistant investigated…", "planned to read the file…", kickoffs and search steps with no result.
- **Questions with no answer** in the episode.
- **Duplicates** of another item in this batch, or of something the conversation already recorded better (prefer the merged, fuller one).
- **Stale snapshots:** a state that later work in the same batch or conversation reversed or superseded.
- One-off task chatter, transient drafts, casual remarks, speculation.
- Facts the code or repository itself already records (function names, file layout), unless the episode adds why.

Watch for misattribution: in a subagent conversation, the "user" turns are the parent agent's instructions. An episode that says "The user decided…" about agent work is wrong; reject it, or approve only if the decision really was the user's and relayed.

## Doc proposals

Approve:
- Durable, reference-shaped knowledge still true read cold later: how something works, a procedure, a standing decision written up as documentation.
- The right home: an existing page when the topic has one, a new page only for a genuinely new topic, in the folder where the user's existing pages on that topic live.

Reject:
- One-off results, point-in-time status, and anything that belongs as an episode instead.
- Pages that duplicate an existing page or another proposal in the batch.
- Snapshots of a design or schema that has since changed.

**The overwrite check is a flag, not a reject rule.** A doc update with `trips_overwrite_check: true` would rewrite more than 30% of its live page (proposals are whole-file rewrites, often written from only a snippet). Read the proposal and the live page (`get_doc_proposal` shows the diff), then:
1. If the live page already holds everything relevant: **reject** as covered.
2. Otherwise: draft an **additive rebuild** with `propose_doc_update` on the full current page (keep its structure, links and frontmatter; add only the missing facts, checked against their source; update the `updated:` date), and recommend **approve** with `rebuild_proposal_id` set to the new proposal. Confirming approves the rebuild and rejects the original.

Frontmatter dates (`created`, `updated`) come from when the conversation happened, not from the review date.

## Always flag rather than decide

- Which project an item belongs to, when it's unclear or would need a new project name.
- A new top-level wiki folder.
- Legal, financial, medical or other personal facts where sources conflict.
- Deleting or replacing substantive existing content.
- Anything touching credentials, private contacts or other people's information.

## Output

End your run by calling `record_review_recommendations` with one entry per item (`label`, `verdict`, `reason`, optional `summary` and `rebuild_proposal_id`), and show the user the summary it returns, unchanged.
