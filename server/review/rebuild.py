"""The rebuild step of the nightly review.

With no human flag step, an item that is part good and part bad can't wait for the user to fix it. The
judge rejects it with a reason that starts "rebuild:" naming what to keep and what to drop or fix, then
calls `rebuild_review_item` with the corrected material. That stages a NEW pending item, which the next
nightly run judges like any other. Nothing here approves anything or touches the wiki or memory.

Guards:
- Only an item the run itself rejected with a "rebuild:" reason can be rebuilt (`rec.verdict == reject`).
- At most one rebuild per item, and a rebuild is never rebuilt: its rationale starts "Rebuild of ", so the
  tool refuses it and the judge must reject it plainly. The loop ends after one round.
- A rebuilt doc passes the same deterministic repairs extraction applies (restated sections, stacked body).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from server.review.recommendations import RecommendationStore

REBUILD_PREFIX = "rebuild:"
MARK = "Rebuild of "


def is_rebuild_reason(reason: Optional[str]) -> bool:
    return (reason or "").strip().lower().startswith(REBUILD_PREFIX)


def rebuild_item(
    store: RecommendationStore,
    run_id: str,
    label: str,
    content: str,
    *,
    target_heading: Optional[str] = None,
    session: Any = None,
    request_id: Any = None,
    wiki_root: Optional[Path] = None,
    proposals_dir: Optional[Path] = None,
    episode_base_dir: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Stage the rebuilt version of one rejected item. Raises ValueError with a message the judge can act on."""
    run = store.run(run_id)
    if run is None:
        raise ValueError(f"Unknown run_id {run_id!r}.")
    if run["recorded_at"] is None:
        raise ValueError("Call record_review_recommendations first; a rebuild follows the rejection it answers.")
    if run["confirmed_at"] or run["closed_at"]:
        raise ValueError(f"Run {run_id} is already confirmed or closed.")
    item = next((i for i in json.loads(run["items_json"]) if i["label"] == label), None)
    if item is None:
        raise ValueError(f"{label!r} is not an item of run {run_id}.")
    rec = next((r for r in store.recommendations(run_id) if r["label"] == label), None)
    if rec is None:
        raise ValueError(f"No recommendation is recorded for {label}.")
    if rec["verdict"] != "reject" or not is_rebuild_reason(rec["reason"]):
        raise ValueError(f"{label} was not rejected with a reason starting 'rebuild:'; only such an item is rebuilt.")
    if rec["rebuilt_as"]:
        raise ValueError(f"{label} was already rebuilt as {rec['rebuilt_as']}; an item is rebuilt once.")
    if not (content or "").strip():
        raise ValueError("content must not be empty.")
    fix = rec["reason"].strip()[len(REBUILD_PREFIX):].strip()

    if item["item_type"] == "doc":
        new_id = _rebuild_doc(item["item_id"], content, fix, target_heading, wiki_root, proposals_dir)
    else:
        new_id = _rebuild_episode(item["item_id"], content, fix, session, request_id, episode_base_dir, db_path)
    store.set_rebuild(run_id, item["item_id"], new_id)
    return {"label": label, "item_type": item["item_type"], "rebuilt_as": new_id}


def _rebuild_doc(proposal_id: str, content: str, fix: str, target_heading: Optional[str],
                 wiki_root: Optional[Path], proposals_dir: Optional[Path]) -> str:
    from server.providers.wiki.corpus import get_corpus_root
    from server.policies.doc_validate import collapse_stacked_body, drop_restated_sections
    from server.proposals import create_doc_proposal, get_proposal, strip_frontmatter, validate_target_path

    orig = get_proposal(proposal_id, proposals_dir)
    if orig is None:
        raise ValueError(f"Proposal {proposal_id} was not found.")
    if (orig.rationale or "").startswith(MARK):
        raise ValueError(f"{proposal_id} is itself a rebuild; it is not rebuilt again. Reject it without 'rebuild:'.")

    root = wiki_root if wiki_root is not None else get_corpus_root()
    target = validate_target_path(orig.target_path, root)
    if target.is_file():
        # an existing page takes only new material; sections it already has are dropped
        content = drop_restated_sections(content, strip_frontmatter(target.read_text(encoding="utf-8", errors="replace")))
        if not content.strip():
            raise ValueError("Everything in content is already on the page; there is nothing new to add.")
    else:
        content = collapse_stacked_body(content)
        if not content.lstrip().startswith("---"):
            raise ValueError("A new page needs complete frontmatter (title, created, updated, status, source, tags).")

    proposal = create_doc_proposal(
        target_path=orig.target_path,
        proposed_content=content,
        rationale=f"{MARK}{proposal_id}: {fix}",
        source_context=orig.source_context,
        source_conversation_id=orig.source_conversation_id,
        source_harness=orig.source_harness,
        source_project=orig.source_project,
        wiki_root=wiki_root,
        proposals_dir=proposals_dir,
        mode="append",
        target_heading=target_heading or orig.target_heading,
        source_date=orig.source_date,
        supersedes=proposal_id,
    )
    return proposal.proposal_id


def _rebuild_episode(memory_id: str, statement: str, fix: str, session: Any, request_id: Any,
                     episode_base_dir: Optional[Path], db_path: Optional[Path]) -> str:
    from server.capture.session_capture import capture_session
    from server.episode_proposals import attach_valid_at, read_episode_mirror

    mirror = read_episode_mirror(memory_id, episode_base_dir)
    if mirror is None:
        raise ValueError(f"Episode {memory_id} was not found.")
    if (mirror.get("rationale") or "").startswith(MARK):
        raise ValueError(f"{memory_id} is itself a rebuild; it is not rebuilt again. Reject it without 'rebuild:'.")
    attach_valid_at([mirror], db_path=db_path)

    item = {
        "destination": "episode",
        "statement": statement.strip(),
        "reasoning_kind": mirror.get("reasoning_kind"),
        "confidence": mirror.get("confidence") if mirror.get("confidence") is not None else 0.8,
        "evidence_text": f"{MARK}{memory_id}: {fix}. Original: {(mirror.get('statement') or '')[:600]}",
        "rationale": f"{MARK}{memory_id}: {fix}",
        "thread_key": mirror.get("thread_key"),
        "driving_question": mirror.get("driving_question"),
        "event_date": mirror.get("valid_at"),
    }
    result = capture_session(items=[item], project=mirror.get("project"), source_description=None,
                             session=session, request_id=request_id, db_path=db_path)[0]
    if not result.ok:
        raise ValueError(result.error or "The rebuilt episode could not be staged.")
    return result.memory_id
