"""Recommended review verdicts, for the nightly auto-review (2026-10-08).

A scheduled task in whatever harness the user chooses (docs/NIGHTLY-REVIEW.md)
reviews what the pollers staged, using CMF's MCP tools:

1. `get_batch` hands it the review rules and the items no run has looked at yet
   (pending tier-1 episodes and doc proposals, oldest first, at most 50), with
   a run id.
2. It judges each item and calls `record`, which stores approve / reject /
   flag recommendations and returns the morning summary: EP and DOC tables,
   flagged items first, and how to confirm. A recommendation changes nothing
   in the review queue, memory or the wiki.
3. In the morning the user confirms in that chat. `confirm` is the only step
   that records real verdicts, through the same review functions a person
   uses. Promotion and doc applies stay separate steps.

A doc update that would rewrite more than 30% of its live page is a cue to
compare, not a reason to reject: the task may draft an additive rebuild with
propose_doc_update and recommend that instead, and confirming approves the
rebuild and rejects the original in one step.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Optional
import uuid

from server.journal.store import DEFAULT_JOURNAL_PATH

VERDICTS = ("approve", "reject", "flag")
MAX_BATCH = 50
RULES_PATH = Path(__file__).with_name("review_rules.md")

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    recorded_at TEXT,
    confirmed_at TEXT,
    item_count INTEGER NOT NULL,
    items_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_recommendations (
    run_id TEXT NOT NULL,
    label TEXT NOT NULL,
    item_type TEXT NOT NULL,
    item_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reason TEXT NOT NULL,
    rebuild_proposal_id TEXT,
    summary TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    final_verdict TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_review_recommendations_item ON review_recommendations(item_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def review_rules() -> str:
    """The generic rules, plus the operator's own (CMF_REVIEW_RULES_PATH)."""
    text = RULES_PATH.read_text(encoding="utf-8").strip()
    extra = os.getenv("CMF_REVIEW_RULES_PATH")
    if extra and extra.strip():
        path = Path(extra).expanduser()
        if path.is_file():
            text += "\n\n## Your own rules\n\n" + path.read_text(encoding="utf-8").strip()
    return text


class RecommendationStore:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_JOURNAL_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "RecommendationStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def seen_item_ids(self) -> set[str]:
        """Items already handed to a run that recorded recommendations."""
        rows = self._conn.execute(
            "SELECT item_id FROM review_recommendations WHERE status IN ('pending', 'confirmed', 'overridden', 'skipped')"
        ).fetchall()
        return {r["item_id"] for r in rows}

    def start_run(self, items: list[dict[str, Any]]) -> str:
        run_id = "rr_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        self._conn.execute(
            "INSERT INTO review_runs (run_id, started_at, item_count, items_json) VALUES (?, ?, ?, ?)",
            (run_id, _now(), len(items), json.dumps([
                {"label": i["label"], "item_type": i["item_type"], "item_id": i["item_id"],
                 "shown": (i.get("target_path") if i["item_type"] == "doc" else i.get("statement")) or i["item_id"]}
                for i in items
            ])),
        )
        self._conn.commit()
        return run_id

    def run(self, run_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM review_runs WHERE run_id = ?", (run_id,)).fetchone()

    def latest_unconfirmed_run(self) -> Optional[str]:
        row = self._conn.execute(
            "SELECT run_id FROM review_runs WHERE recorded_at IS NOT NULL AND confirmed_at IS NULL "
            "ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
        return row["run_id"] if row else None

    def unconfirmed_runs(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT run_id FROM review_runs WHERE recorded_at IS NOT NULL AND confirmed_at IS NULL ORDER BY recorded_at"
        ).fetchall()
        return [r["run_id"] for r in rows]

    def save(self, run_id: str, recs: list[dict[str, Any]]) -> None:
        now = _now()
        with self._conn:
            for r in recs:
                self._conn.execute(
                    """INSERT OR REPLACE INTO review_recommendations
                       (run_id, label, item_type, item_id, verdict, reason, rebuild_proposal_id, summary, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                    (run_id, r["label"], r["item_type"], r["item_id"], r["verdict"], r["reason"],
                     r.get("rebuild_proposal_id"), r.get("summary"), now),
                )
            self._conn.execute("UPDATE review_runs SET recorded_at = ? WHERE run_id = ?", (now, run_id))

    def recommendations(self, run_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM review_recommendations WHERE run_id = ? ORDER BY item_type DESC, CAST(SUBSTR(label, 3) AS INTEGER), label",
            (run_id,),
        ).fetchall()

    def decide(self, run_id: str, item_id: str, status: str, final_verdict: Optional[str]) -> None:
        self._conn.execute(
            "UPDATE review_recommendations SET status = ?, final_verdict = ?, decided_at = ? WHERE run_id = ? AND item_id = ?",
            (status, final_verdict, _now(), run_id, item_id),
        )
        self._conn.commit()

    def mark_confirmed(self, run_id: str) -> None:
        self._conn.execute("UPDATE review_runs SET confirmed_at = ? WHERE run_id = ?", (_now(), run_id))
        self._conn.commit()


# --- batch ------------------------------------------------------------------------


def _doc_coverage(proposal: Any, wiki_root: Optional[Path]) -> dict[str, Any]:
    """For an update: how much of the live page the proposal would rewrite."""
    from server.proposals import UPDATE_GUARD_MIN_LINES, UPDATE_MAX_REMOVED_FRACTION, removed_line_fraction

    if proposal.operation != "update" or wiki_root is None:
        return {}
    live = wiki_root / proposal.target_path
    if not live.is_file():
        return {"live_page": "missing"}
    text = live.read_text(encoding="utf-8", errors="replace")
    lines = len(text.splitlines())
    frac = removed_line_fraction(text, proposal.proposed_content)
    return {
        "live_page_lines": lines,
        "rewrites_fraction": round(frac, 2),
        "trips_overwrite_check": lines >= UPDATE_GUARD_MIN_LINES and frac > UPDATE_MAX_REMOVED_FRACTION,
    }


def build_batch(
    store: RecommendationStore,
    *,
    max_items: int = MAX_BATCH,
    episode_base_dir: Optional[Path] = None,
    proposals_dir: Optional[Path] = None,
    wiki_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Start a run over the pending items no earlier run has recommended on."""
    from server.episode_proposals import list_episode_mirrors
    from server.proposals import list_proposals

    max_items = max(1, min(int(max_items), MAX_BATCH))
    seen = store.seen_item_ids()

    episodes = [
        m for m in list_episode_mirrors(tier="tier1", approval_state="queued_for_review", base_dir=episode_base_dir)
        if m.get("memory_id") and m["memory_id"] not in seen
    ]
    episodes.sort(key=lambda m: m.get("written_at") or "")
    docs = [p for p in list_proposals(status="pending_review", proposals_dir=proposals_dir) if p.proposal_id not in seen]
    docs.sort(key=lambda p: p.created_at or "")

    # Docs are fewer and slower to judge; give them up to a fifth of the batch.
    doc_take = min(len(docs), max(max_items // 5, max_items - len(episodes)))
    ep_take = min(len(episodes), max_items - doc_take)

    items: list[dict[str, Any]] = []
    for i, m in enumerate(episodes[:ep_take], 1):
        items.append({
            "label": f"EP{i}",
            "item_type": "episode",
            "item_id": m["memory_id"],
            "kind": m.get("reasoning_kind"),
            "statement": m.get("statement"),
            "driving_question": m.get("driving_question"),
            "rationale": m.get("rationale"),
            "project": m.get("project"),
            "harness": m.get("harness"),
            "conversation_id": m.get("conversation_id"),
            "thread_key": m.get("thread_key"),
            "evidence_turns": len(m.get("evidence_event_ids") or []),
            "written_at": m.get("written_at"),
        })
    for i, p in enumerate(docs[:doc_take], 1):
        items.append({
            "label": f"DOC{i}",
            "item_type": "doc",
            "item_id": p.proposal_id,
            "operation": p.operation,
            "target_path": p.target_path,
            "statement": getattr(p, "statement", None) or p.rationale[:300],
            "rationale": p.rationale,
            "project": getattr(p, "source_project", None),
            "conversation_id": getattr(p, "source_conversation_id", None),
            "created_at": p.created_at,
            **_doc_coverage(p, wiki_root),
        })

    run_id = store.start_run(items) if items else None
    return {
        "run_id": run_id,
        "rules": review_rules(),
        "items": items,
        "remaining": {"episodes": len(episodes) - ep_take, "docs": len(docs) - doc_take},
        "unconfirmed_runs": store.unconfirmed_runs(),
    }


# --- record -----------------------------------------------------------------------


def record(store: RecommendationStore, run_id: str, recommendations: list[dict[str, Any]],
           proposals_dir: Optional[Path] = None) -> dict[str, Any]:
    """Validate and store a run's recommendations; return the morning summary."""
    from server.proposals import get_proposal

    run = store.run(run_id)
    if run is None:
        raise ValueError(f"Unknown run_id {run_id!r}. Start one with get_review_batch.")
    if run["confirmed_at"]:
        raise ValueError(f"Run {run_id} is already confirmed.")
    items = {i["label"]: i for i in json.loads(run["items_json"])}
    by_id = {i["item_id"]: i for i in items.values()}

    saved, errors = [], []
    for rec in recommendations:
        key = rec.get("label") or rec.get("item_id")
        item = items.get(key) or by_id.get(key)
        verdict = (rec.get("verdict") or "").strip().lower()
        reason = (rec.get("reason") or "").strip()
        if item is None:
            errors.append(f"{key!r}: not an item of run {run_id}")
            continue
        if verdict not in VERDICTS:
            errors.append(f"{item['label']}: verdict {verdict!r} is not one of {', '.join(VERDICTS)}")
            continue
        if not reason:
            errors.append(f"{item['label']}: a reason is required")
            continue
        rebuild = rec.get("rebuild_proposal_id")
        if rebuild:
            if item["item_type"] != "doc":
                errors.append(f"{item['label']}: only a doc can carry a rebuild_proposal_id")
                continue
            proposal = get_proposal(rebuild, proposals_dir)
            if proposal is None or proposal.status != "pending_review":
                errors.append(f"{item['label']}: rebuild {rebuild!r} is not a pending proposal")
                continue
        shown = item.get("shown")
        if item["item_type"] == "episode":
            shown = (rec.get("summary") or "").strip() or shown
        saved.append({**item, "verdict": verdict, "reason": reason, "rebuild_proposal_id": rebuild, "summary": shown})

    missing = [label for label, i in items.items() if i["item_id"] not in {s["item_id"] for s in saved}]
    store.save(run_id, saved)
    return {"run_id": run_id, "recorded": len(saved), "errors": errors, "not_recommended": missing,
            "summary": format_summary(store, run_id)}


# --- summary ----------------------------------------------------------------------


def _cell(text: Optional[str], limit: int = 180) -> str:
    text = " ".join((text or "").split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


_VERDICT_WORD = {"approve": "**Approve.**", "reject": "**Reject.**", "flag": "**Flag.**"}


def format_summary(store: RecommendationStore, run_id: str) -> str:
    recs = store.recommendations(run_id)
    if not recs:
        return f"Run `{run_id}`: no recommendations recorded."
    order = {"flag": 0, "approve": 1, "reject": 2}
    eps = sorted((r for r in recs if r["item_type"] == "episode"), key=lambda r: (order[r["verdict"]], int(r["label"][2:])))
    docs = sorted((r for r in recs if r["item_type"] == "doc"), key=lambda r: (order[r["verdict"]], int(r["label"][3:])))
    counts = {v: sum(1 for r in recs if r["verdict"] == v) for v in VERDICTS}
    pending = sum(1 for r in recs if r["status"] == "pending")
    out = [f"### Nightly review `{run_id}`",
           f"{len(eps)} EP, {len(docs)} DOC: {counts['approve']} approve, {counts['reject']} reject, {counts['flag']} flagged for you. "
           f"{pending} awaiting your confirmation; nothing has been changed yet."]
    if eps:
        out += ["", "**Episodes (EP)**", "", "| # | Statement | Reasoning |", "|---|---|---|"]
        for r in eps:
            out.append(f"| {r['label']} | {_cell(r['summary'])} | {_VERDICT_WORD[r['verdict']]} {_cell(r['reason'], 240)} |")
    if docs:
        out += ["", "**Doc proposals (DOC)**", "", "| # | Target path | Rationale |", "|---|---|---|"]
        for r in docs:
            rebuild = f" Rebuild drafted as `{r['rebuild_proposal_id']}`." if r["rebuild_proposal_id"] else ""
            out.append(f"| {r['label']} | `{r['summary']}` | {_VERDICT_WORD[r['verdict']]} {_cell(r['reason'], 240)}{rebuild} |")
    out += ["", f"To confirm: `confirm_review_recommendations(run_id=\"{run_id}\")`, or say e.g. "
            "\"confirm, but EP3 approve and skip DOC1\". Flagged items stay in the queue unless you give them a verdict. "
            "Confirming records the verdicts only; promotion and doc applies stay separate."]
    return "\n".join(out)


# --- confirm ----------------------------------------------------------------------


def confirm(
    store: RecommendationStore,
    run_id: Optional[str] = None,
    overrides: Optional[dict[str, str]] = None,
    skip: Optional[list[str]] = None,
    *,
    reviewer: Optional[str] = None,
    review_db: Optional[Path] = None,
    proposals_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Record the user's verdicts for a run: its recommendations, with overrides.

    `overrides` maps a label (EP3, DOC1) or item id to approve / reject;
    `skip` leaves those items in the queue. A flagged item without an
    override is left in the queue too.
    """
    from server.core.config import default_reviewer
    from server.proposals import get_proposal, review_proposal
    from server.review import actions
    from server.review.store import ReviewStore

    run_id = run_id or store.latest_unconfirmed_run()
    if run_id is None:
        raise ValueError("No recorded, unconfirmed review run.")
    run = store.run(run_id)
    if run is None:
        raise ValueError(f"Unknown run_id {run_id!r}.")
    reviewer = reviewer or default_reviewer()
    overrides = {k.strip().upper() if k.strip().upper().startswith(("EP", "DOC")) else k.strip(): v.strip().lower()
                 for k, v in (overrides or {}).items()}
    skip_keys = {s.strip().upper() if s.strip().upper().startswith(("EP", "DOC")) else s.strip() for s in (skip or [])}
    bad = [f"{k}: {v!r}" for k, v in overrides.items() if v not in ("approve", "reject")]
    if bad:
        raise ValueError("Overrides must be approve or reject: " + ", ".join(bad))

    done: dict[str, list[str]] = {"approved": [], "rejected": [], "left_in_queue": [], "already_decided": [], "errors": []}
    with ReviewStore(review_db) as rs:
        for r in store.recommendations(run_id):
            label, item_id = r["label"], r["item_id"]
            if r["status"] != "pending":
                done["already_decided"].append(label)
                continue
            if label in skip_keys or item_id in skip_keys:
                store.decide(run_id, item_id, "skipped", None)
                done["left_in_queue"].append(label)
                continue
            final = overrides.get(label) or overrides.get(item_id) or (r["verdict"] if r["verdict"] != "flag" else None)
            if final is None:
                done["left_in_queue"].append(label)
                continue
            reason = f"Nightly review {run_id}, confirmed by {reviewer}: {r['reason']}"
            if final != r["verdict"]:
                reason = f"Nightly review {run_id}: {reviewer} changed {r['verdict']} to {final}. Recommendation was: {r['reason']}"
            try:
                if r["item_type"] == "episode":
                    fn = actions.approve_episode if final == "approve" else actions.reject_episode
                    fn(rs, item_id, reviewer=reviewer, reason=reason)
                else:
                    _confirm_doc(item_id, r["rebuild_proposal_id"], final, reviewer, reason, proposals_dir,
                                 get_proposal, review_proposal)
            except Exception as exc:  # one bad item must not sink the batch
                done["errors"].append(f"{label}: {exc}")
                continue
            store.decide(run_id, item_id, "confirmed" if final == r["verdict"] else "overridden", final)
            done["approved" if final == "approve" else "rejected"].append(label)

    if not any(r["status"] == "pending" and r["verdict"] != "flag" for r in store.recommendations(run_id)):
        store.mark_confirmed(run_id)
    return {"run_id": run_id, **done}


def _confirm_doc(item_id, rebuild_id, final, reviewer, reason, proposals_dir, get_proposal, review_proposal) -> None:
    original = get_proposal(item_id, proposals_dir)
    if original is None:
        raise ValueError(f"proposal {item_id} not found")
    if final == "approve" and rebuild_id:
        review_proposal(rebuild_id, "approved", reviewer=reviewer, notes=reason, proposals_dir=proposals_dir)
        if original.status == "pending_review":
            review_proposal(item_id, "rejected", reviewer=reviewer, notes=f"Rebuilt non-destructively as {rebuild_id}.",
                            proposals_dir=proposals_dir)
        return
    if original.status == "pending_review":
        review_proposal(item_id, "approved" if final == "approve" else "rejected", reviewer=reviewer, notes=reason,
                        proposals_dir=proposals_dir)
    if final == "reject" and rebuild_id:
        rebuilt = get_proposal(rebuild_id, proposals_dir)
        if rebuilt is not None and rebuilt.status == "pending_review":
            review_proposal(rebuild_id, "rejected", reviewer=reviewer, notes=reason, proposals_dir=proposals_dir)
