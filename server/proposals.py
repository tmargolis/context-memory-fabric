"""Durable knowledge proposal store and validation for Context Memory Fabric.

Manages pending update/creation proposals for LLM_Wiki without modifying the
canonical corpus. Proposals persist locally under doc-proposals/ for human
review. Named generically (`DocProposal`, not `WikiProposal`) because the
proposal/review lifecycle itself is provider-agnostic -- LLM_Wiki is the
only knowledge provider that exists today (see MS5 in docs/plan-active.md),
but nothing in this module's shape assumes it stays that way. The apply
step's actual corpus writes (`wiki_root`, "LLM_Wiki") remain wiki-specific
implementation, deliberately -- generalizing *that* is MS5's job, not this
rename's (docs/plan-active.md, "Wiki→doc rename", 2026-09-19).
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import difflib
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional
import uuid

from server.corpus import (
    IGNORED_DIR_NAMES,
    get_corpus_root,
    is_excluded_path,
)

logger = logging.getLogger(__name__)

# Supported text file extensions for wiki update proposals
SUPPORTED_TEXT_EXTENSIONS = {
    ".md",
    ".markdown",
    ".txt",
    ".json",
    ".csv",
    ".yaml",
    ".yml",
    ".html",
    ".sh",
    ".py",
    ".base",
}


@dataclass
class DocProposal:
    """Record representing a pending proposal to create or update a Wiki file."""

    proposal_id: str
    status: str = "pending_review"
    operation: str = "update"  # "create" | "update"
    target_path: str = ""
    created_at: str = ""
    rationale: str = ""
    source_context: Optional[str] = None
    current_sha256: Optional[str] = None
    proposed_sha256: str = ""
    proposed_content: str = ""
    unified_diff: str = ""
    # MS6d — review/apply state. Absent on any of the 76 pre-MS6d proposal
    # files; every field here defaults to None so from_dict(**data) loads
    # them unchanged rather than needing a migration pass.
    reviewer: Optional[str] = None
    reviewed_at: Optional[str] = None
    review_notes: Optional[str] = None
    applied_at: Optional[str] = None
    applied_commit_sha: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DocProposal":
        return cls(**data)


# Terminal review verdicts. "applied" is a separate status apply_proposal()
# sets on success — never accepted directly as a review verdict, since that
# would let review_proposal() claim a canonical write happened without one.
REVIEW_VERDICTS = ("approved", "rejected")


def get_proposals_dir(custom_dir: Optional[Path] = None) -> Path:
    """Return the directory where doc proposals are stored locally."""
    if custom_dir is not None:
        p = custom_dir
    else:
        env_state_dir = os.getenv("CMF_STATE_DIR")
        if env_state_dir:
            p = Path(env_state_dir).expanduser().resolve() / "doc-proposals"
        else:
            # Default to <project_root>/doc-proposals
            project_root = Path(__file__).resolve().parent.parent
            p = project_root / "doc-proposals"

    p.mkdir(parents=True, exist_ok=True)
    return p


def _status_subdir(status: str) -> Optional[str]:
    """Which subfolder a proposal's file lives in for a given status.

    None means the flat root (still `pending_review`). `applied` is a
    sub-state of `approved` (MS6d's own vocabulary: pending_review ->
    approved/rejected -> applied, approved-only) so it stays under
    `approved/` rather than getting a third folder -- found 2026-09-18,
    the flat root had grown to 78 files with no way to tell reviewed from
    unreviewed at a glance.
    """
    if status in ("approved", "applied"):
        return "approved"
    if status == "rejected":
        return "rejected"
    return None


def _locate_proposal_file(proposal_id: str, store_dir: Path) -> Optional[Path]:
    """Find an existing proposal file by id, checking the flat root then both subfolders."""
    for candidate in (
        store_dir / f"{proposal_id}.json",
        store_dir / "approved" / f"{proposal_id}.json",
        store_dir / "rejected" / f"{proposal_id}.json",
    ):
        if candidate.exists():
            return candidate
    return None


def validate_target_path(target_path: str, wiki_root: Path) -> Path:
    """Validate that the target path is safe and refers to a supported text file within LLM_Wiki.

    Raises:
        ValueError: If path is absolute, traverses out of root, targets git/ignored
            folders, or has an unsupported binary extension.
    """
    clean = target_path.strip()
    if not clean:
        raise ValueError("Target path cannot be empty.")

    p = Path(clean)
    if p.is_absolute() or clean.startswith("/"):
        raise ValueError(f"Target path must be relative to LLM_Wiki root, got absolute path: '{clean}'")

    for part in p.parts:
        if part == ".." or part == ".":
            raise ValueError(f"Path traversal ('{part}') is not allowed: '{clean}'")
        if part.startswith(".git") or part in IGNORED_DIR_NAMES:
            raise ValueError(f"Targeting internal/ignored directory ('{part}') is not allowed: '{clean}'")

    resolved = (wiki_root / p).resolve()

    try:
        rel = resolved.relative_to(wiki_root.resolve())
    except ValueError:
        raise ValueError(f"Target path escapes corpus root: '{clean}'")

    if is_excluded_path(rel):
        raise ValueError(f"Target path is within an excluded area: '{rel.as_posix()}'")

    ext = resolved.suffix.lower()
    if ext not in SUPPORTED_TEXT_EXTENSIONS:
        raise ValueError(
            f"Proposals are only supported for textual knowledge files ({', '.join(sorted(SUPPORTED_TEXT_EXTENSIONS))}), "
            f"not binary/media assets ('{ext}'). Target: '{clean}'"
        )

    return resolved


def create_doc_proposal(
    target_path: str,
    proposed_content: str,
    rationale: str,
    source_context: Optional[str] = None,
    wiki_root: Optional[Path] = None,
    proposals_dir: Optional[Path] = None,
) -> DocProposal:
    """Create and persist a new reviewable Wiki update proposal.

    Does NOT modify the target file in LLM_Wiki.
    """
    root = wiki_root if wiki_root is not None else get_corpus_root()
    resolved_target = validate_target_path(target_path, root)
    rel_path_str = resolved_target.relative_to(root.resolve()).as_posix()

    now_iso = datetime.now(timezone.utc).isoformat()
    prop_id = f"prop_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    proposed_bytes = proposed_content.encode("utf-8")
    proposed_sha = hashlib.sha256(proposed_bytes).hexdigest()

    if resolved_target.exists():
        if not resolved_target.is_file():
            raise ValueError(f"Target path exists but is not a file: '{rel_path_str}'")

        operation = "update"
        current_text = resolved_target.read_text(encoding="utf-8", errors="replace")
        current_sha = hashlib.sha256(current_text.encode("utf-8")).hexdigest()

        diff_lines = list(
            difflib.unified_diff(
                current_text.splitlines(keepends=True),
                proposed_content.splitlines(keepends=True),
                fromfile=f"a/{rel_path_str}",
                tofile=f"b/{rel_path_str}",
                lineterm="",
            )
        )
        unified_diff_str = "\n".join(diff_lines)
    else:
        operation = "create"
        current_sha = None

        diff_lines = list(
            difflib.unified_diff(
                [],
                proposed_content.splitlines(keepends=True),
                fromfile="/dev/null",
                tofile=f"b/{rel_path_str}",
                lineterm="",
            )
        )
        unified_diff_str = "\n".join(diff_lines)

    if not unified_diff_str.strip():
        unified_diff_str = f"(No textual differences between current and proposed content for {rel_path_str})"

    proposal = DocProposal(
        proposal_id=prop_id,
        status="pending_review",
        operation=operation,
        target_path=rel_path_str,
        created_at=now_iso,
        rationale=rationale.strip(),
        source_context=source_context.strip() if source_context else None,
        current_sha256=current_sha,
        proposed_sha256=proposed_sha,
        proposed_content=proposed_content,
        unified_diff=unified_diff_str,
    )

    # Persist to local JSON file (pending_review -> flat root, via _save_proposal)
    _save_proposal(proposal, proposals_dir)
    logger.info(f"Saved doc proposal '{prop_id}'")

    return proposal


def format_proposal_for_mcp(proposal: DocProposal) -> str:
    """Format a DocProposal into clear Markdown for MCP tool responses."""
    lines = [
        "### 📝 Doc Update Proposal Generated\n",
        f"- **Proposal ID:** `{proposal.proposal_id}`",
        f"- **Status:** `{proposal.status}`",
        f"- **Operation:** `{proposal.operation.upper()}`",
        f"- **Target Path:** `{proposal.target_path}`",
        f"- **Created At:** `{proposal.created_at}`",
        f"- **Target Current SHA-256:** `{proposal.current_sha256 or 'N/A (New File)'}`",
        f"- **Proposed SHA-256:** `{proposal.proposed_sha256}`",
        f"- **Rationale:** {proposal.rationale}",
    ]

    if proposal.source_context:
        lines.append(f"- **Source Context:** {proposal.source_context}")

    lines.append("\n#### Proposed Changes (Unified Diff)")
    lines.append("```diff")
    lines.append(proposal.unified_diff)
    lines.append("```\n")

    lines.append("> [!IMPORTANT]")
    lines.append("> This proposal is saved locally for review and has **NOT** modified `LLM_Wiki`.")

    return "\n".join(lines)


def list_proposals(
    proposals_dir: Optional[Path] = None,
    status: Optional[str] = None,
) -> list[DocProposal]:
    """List stored proposals from the local state directory (flat root +
    approved/ + rejected/ subfolders -- see _status_subdir)."""
    store_dir = get_proposals_dir(proposals_dir)
    proposals: list[DocProposal] = []

    for search_dir in (store_dir, store_dir / "approved", store_dir / "rejected"):
        if not search_dir.exists():
            continue
        for item in search_dir.glob("*.json"):
            try:
                data = json.loads(item.read_text(encoding="utf-8"))
                prop = DocProposal.from_dict(data)
                if status is None or prop.status == status:
                    proposals.append(prop)
            except Exception as e:
                logger.warning(f"Failed loading proposal file {item}: {e}")

    proposals.sort(key=lambda p: p.proposal_id)
    return proposals


def get_proposal(
    proposal_id: str,
    proposals_dir: Optional[Path] = None,
) -> Optional[DocProposal]:
    """Retrieve a single proposal by ID, wherever its status has it filed."""
    store_dir = get_proposals_dir(proposals_dir)
    target = _locate_proposal_file(proposal_id, store_dir)
    if target is None:
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return DocProposal.from_dict(data)
    except Exception as e:
        logger.warning(f"Failed loading proposal {target}: {e}")
        return None


def _save_proposal(proposal: DocProposal, proposals_dir: Optional[Path] = None) -> None:
    """Write a proposal to the subfolder its current status belongs in
    (_status_subdir), moving it there if it currently lives somewhere else
    -- a proposal never exists in two places at once."""
    store_dir = get_proposals_dir(proposals_dir)
    subdir = _status_subdir(proposal.status)
    target_dir = (store_dir / subdir) if subdir else store_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    new_path = target_dir / f"{proposal.proposal_id}.json"

    existing = _locate_proposal_file(proposal.proposal_id, store_dir)
    if existing is not None and existing != new_path:
        existing.unlink()

    new_path.write_text(json.dumps(proposal.to_dict(), indent=2), encoding="utf-8")


def review_proposal(
    proposal_id: str,
    verdict: str,
    reviewer: str = "todd",
    notes: Optional[str] = None,
    proposals_dir: Optional[Path] = None,
) -> DocProposal:
    """Record a human decision on a pending proposal. Does NOT touch LLM_Wiki.

    This is the split MS6d's design relies on for safety: a verdict here is a
    decision, not a write. Only `apply_proposal()` — and only on a proposal
    already `approved` here — ever touches the corpus.

    Raises:
        ValueError: unknown proposal_id, unknown verdict, or the proposal is
            not currently `pending_review` (re-reviewing an already-decided
            proposal is refused rather than silently overwriting the first
            verdict — re-open by editing the JSON directly if that's truly
            intended, which is deliberately not a one-call operation).
    """
    if verdict not in REVIEW_VERDICTS:
        raise ValueError(f"Unknown verdict '{verdict}'; must be one of {REVIEW_VERDICTS}.")

    proposal = get_proposal(proposal_id, proposals_dir)
    if proposal is None:
        raise ValueError(f"No proposal found with id '{proposal_id}'.")
    if proposal.status != "pending_review":
        raise ValueError(
            f"Proposal '{proposal_id}' is already '{proposal.status}', not 'pending_review' — "
            "re-review is refused rather than overwriting the prior verdict."
        )

    proposal.status = verdict
    proposal.reviewer = reviewer
    proposal.reviewed_at = datetime.now(timezone.utc).isoformat()
    proposal.review_notes = notes.strip() if notes else None

    _save_proposal(proposal, proposals_dir)
    logger.info(f"Proposal '{proposal_id}' reviewed: {verdict} by {reviewer}")
    return proposal


def bulk_reject_proposals(
    proposal_ids: list[str],
    reason: str,
    reviewer: str = "todd",
    proposals_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Reject a batch of pending proposals with one recorded reason each.

    Mirrors the episodic side's bulk-reject shape (server/review/actions.py):
    one reason applied across a set the caller has already filtered (e.g. via
    list_proposals(status="pending_review")), each still individually
    recorded rather than one bulk row.
    """
    rejected: list[str] = []
    skipped: list[dict[str, str]] = []
    for pid in proposal_ids:
        try:
            review_proposal(pid, "rejected", reviewer=reviewer, notes=reason, proposals_dir=proposals_dir)
            rejected.append(pid)
        except ValueError as e:
            skipped.append({"proposal_id": pid, "reason": str(e)})
    return {"rejected": rejected, "skipped": skipped}


def apply_proposal(
    proposal_id: str,
    expected_sha256: Optional[str] = None,
    dry_run: bool = True,
    wiki_root: Optional[Path] = None,
    proposals_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Write an approved proposal's content into LLM_Wiki. The only function
    in this module that touches the canonical corpus.

    Two independent guards, both required:
    - `expected_sha256` must match `proposal.proposed_sha256` — forces the
      caller to have actually re-fetched this proposal (via get_proposal)
      before applying it, not be acting on a stale in-context copy.
    - The live target file's current hash must match `proposal.current_sha256`
      (the base it was diffed against at creation time) for an "update", or
      must not exist for a "create". Either drift is refused by name, not
      silently overwritten — with 76 proposals up to weeks old, this is a
      real case, not a theoretical one.

    On a real (non-dry-run) apply: writes proposed_content to the resolved
    target, commits it in the LLM_Wiki git repo if one is present (best
    effort — a missing git repo does not block the apply, since the write
    itself is the durable action), and marks the proposal 'applied'.

    Raises:
        ValueError: unknown proposal_id, not yet 'approved', sha mismatch on
            either guard, or a "create" whose target now exists.
    """
    proposal = get_proposal(proposal_id, proposals_dir)
    if proposal is None:
        raise ValueError(f"No proposal found with id '{proposal_id}'.")
    if proposal.status != "approved":
        raise ValueError(
            f"Proposal '{proposal_id}' is '{proposal.status}', not 'approved' — "
            "review it first via review_proposal(verdict='approved')."
        )
    if expected_sha256 and expected_sha256 != proposal.proposed_sha256:
        raise ValueError(
            f"expected_sha256 does not match this proposal's proposed content "
            f"(expected {proposal.proposed_sha256}, got {expected_sha256}) — "
            "re-fetch via get_proposal() before applying."
        )

    root = wiki_root if wiki_root is not None else get_corpus_root()
    resolved_target = validate_target_path(proposal.target_path, root)

    if proposal.operation == "create":
        if resolved_target.exists():
            raise ValueError(
                f"Proposal '{proposal_id}' is a CREATE for '{proposal.target_path}', "
                "but that file now exists — created by something else since this "
                "proposal was made. Refusing to overwrite."
            )
    else:  # "update"
        if not resolved_target.exists():
            raise ValueError(
                f"Proposal '{proposal_id}' is an UPDATE for '{proposal.target_path}', "
                "but that file no longer exists. Refusing to apply against a moved target."
            )
        live_text = resolved_target.read_text(encoding="utf-8", errors="replace")
        live_sha = hashlib.sha256(live_text.encode("utf-8")).hexdigest()
        if live_sha != proposal.current_sha256:
            raise ValueError(
                f"Proposal '{proposal_id}' was diffed against sha {proposal.current_sha256}, "
                f"but '{proposal.target_path}' now hashes to {live_sha} — it changed since "
                "this proposal was created. Refusing to apply against a stale base; "
                "create a fresh proposal against the current content instead."
            )

    if dry_run:
        return {
            "dry_run": True,
            "proposal_id": proposal_id,
            "would_write": proposal.target_path,
            "operation": proposal.operation,
        }

    resolved_target.parent.mkdir(parents=True, exist_ok=True)
    resolved_target.write_text(proposal.proposed_content, encoding="utf-8")

    commit_sha = _git_commit_change(root, resolved_target, proposal)

    proposal.status = "applied"
    proposal.applied_at = datetime.now(timezone.utc).isoformat()
    proposal.applied_commit_sha = commit_sha
    _save_proposal(proposal, proposals_dir)
    logger.info(f"Proposal '{proposal_id}' applied to '{proposal.target_path}' (commit {commit_sha})")

    return {
        "dry_run": False,
        "proposal_id": proposal_id,
        "wrote": proposal.target_path,
        "operation": proposal.operation,
        "commit_sha": commit_sha,
    }


def _git_commit_change(wiki_root: Path, resolved_target: Path, proposal: DocProposal) -> Optional[str]:
    """Best-effort commit of an applied proposal inside the LLM_Wiki repo.

    Not required for the apply to succeed — the file write is the durable
    action, this is the free undo path on top of it. Returns the new commit
    sha, or None if wiki_root isn't a git repo or the commit otherwise fails
    (logged, not raised).
    """
    import subprocess

    try:
        rel = resolved_target.relative_to(wiki_root.resolve())
        subprocess.run(
            ["git", "-C", str(wiki_root), "add", "--", str(rel)],
            check=True, capture_output=True, text=True,
        )
        message = f"proposal {proposal.proposal_id}: {proposal.operation} {rel.as_posix()}\n\n{proposal.rationale}"
        subprocess.run(
            ["git", "-C", str(wiki_root), "commit", "-m", message],
            check=True, capture_output=True, text=True,
        )
        result = subprocess.run(
            ["git", "-C", str(wiki_root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        )
        return result.stdout.strip()
    except Exception as e:
        logger.warning(f"Best-effort git commit skipped/failed for proposal {proposal.proposal_id}: {e}")
        return None


def format_proposal_list(proposals: list[DocProposal]) -> str:
    """Format a list of proposals as a compact Markdown table for MCP tool responses."""
    if not proposals:
        return "No proposals found."
    lines = ["| Proposal ID | Status | Operation | Target Path | Created |", "|---|---|---|---|---|"]
    for p in proposals:
        lines.append(f"| `{p.proposal_id}` | {p.status} | {p.operation} | `{p.target_path}` | {p.created_at} |")
    return "\n".join(lines)


def format_review_result(proposal: DocProposal) -> str:
    lines = [
        f"### Proposal `{proposal.proposal_id}` — {proposal.status}",
        f"- **Reviewer:** {proposal.reviewer}",
        f"- **Reviewed At:** {proposal.reviewed_at}",
    ]
    if proposal.review_notes:
        lines.append(f"- **Notes:** {proposal.review_notes}")
    if proposal.status == "approved":
        lines.append(
            f"\n> Ready to apply. Call `apply_doc_proposal(proposal_id=\"{proposal.proposal_id}\", "
            f"expected_sha256=\"{proposal.proposed_sha256}\")` with `dry_run=True` first."
        )
    return "\n".join(lines)


def format_apply_result(result: dict[str, Any]) -> str:
    if result["dry_run"]:
        return (
            f"### 🔍 Dry run — nothing written\n"
            f"Would **{result['operation'].upper()}** `{result['would_write']}`.\n\n"
            f"Re-call with `dry_run=False` to actually apply."
        )
    commit_note = f" (commit `{result['commit_sha']}`)" if result["commit_sha"] else " (no git commit — target is not a git repo, or commit failed; see server logs)"
    return (
        f"### ✅ Applied\n"
        f"**{result['operation'].upper()}** wrote `{result['wrote']}`{commit_note}."
    )
