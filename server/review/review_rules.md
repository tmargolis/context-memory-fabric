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

**Judge what a doc adds.** A doc with `mode: "append"` adds `addition` to the live page, under `target_heading` when set (otherwise at the end); the rest of the page is unchanged and its `created:` date is kept. Judge the addition: reject it if the page already covers it, if it belongs on a different page, or if it is a new topic that should be its own page. A doc with `mode: "replace"` is a deliberate whole-page rewrite: read it against the live page (`get_doc_proposal` shows the diff) and flag it if it removes substantive content.

**Conflicts stay separate.** `conflicts_with` names other pending proposals for the same page, often from another conversation. Judge each on its own; when two say incompatible things, flag rather than pick.

Frontmatter dates (`created`, `updated`) come from when the conversation happened, not from the review date.

## Always flag rather than decide

- Which project an item belongs to, when it's unclear or would need a new project name.
- A new top-level wiki folder.
- Legal, financial, medical or other personal facts where sources conflict.
- Deleting or replacing substantive existing content.
- Anything touching credentials, private contacts or other people's information.

## Output

End your run by calling `record_review_recommendations` with one entry per item (`label`, `verdict`, `reason`, optional `summary`), and show the user the summary it returns, unchanged.
