"""Durable knowledge proposal store and validation for Context Memory Fabric.

Manages pending update/creation proposals for LLM_Wiki without modifying the
canonical corpus. Proposals persist locally under .cmf/wiki-proposals/ for human
review.
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
class WikiProposal:
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WikiProposal":
        return cls(**data)


def get_proposals_dir(custom_dir: Optional[Path] = None) -> Path:
    """Return the directory where wiki proposals are stored locally."""
    if custom_dir is not None:
        p = custom_dir
    else:
        env_state_dir = os.getenv("CMF_STATE_DIR")
        if env_state_dir:
            p = Path(env_state_dir).expanduser().resolve() / "wiki-proposals"
        else:
            # Default to <project_root>/.cmf/wiki-proposals
            project_root = Path(__file__).resolve().parent.parent
            p = project_root / ".cmf" / "wiki-proposals"

    p.mkdir(parents=True, exist_ok=True)
    return p


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


def create_wiki_proposal(
    target_path: str,
    proposed_content: str,
    rationale: str,
    source_context: Optional[str] = None,
    wiki_root: Optional[Path] = None,
    proposals_dir: Optional[Path] = None,
) -> WikiProposal:
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

    proposal = WikiProposal(
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

    # Persist to local JSON file
    store_dir = get_proposals_dir(proposals_dir)
    file_path = store_dir / f"{prop_id}.json"
    file_path.write_text(json.dumps(proposal.to_dict(), indent=2), encoding="utf-8")
    logger.info(f"Saved wiki proposal '{prop_id}' to {file_path}")

    return proposal


def format_proposal_for_mcp(proposal: WikiProposal) -> str:
    """Format a WikiProposal into clear Markdown for MCP tool responses."""
    lines = [
        "### 📝 Wiki Update Proposal Generated\n",
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
) -> list[WikiProposal]:
    """List stored proposals from the local state directory."""
    store_dir = get_proposals_dir(proposals_dir)
    proposals: list[WikiProposal] = []

    for item in sorted(store_dir.glob("*.json")):
        try:
            data = json.loads(item.read_text(encoding="utf-8"))
            prop = WikiProposal.from_dict(data)
            if status is None or prop.status == status:
                proposals.append(prop)
        except Exception as e:
            logger.warning(f"Failed loading proposal file {item}: {e}")

    return proposals


def get_proposal(
    proposal_id: str,
    proposals_dir: Optional[Path] = None,
) -> Optional[WikiProposal]:
    """Retrieve a single proposal by ID."""
    store_dir = get_proposals_dir(proposals_dir)
    target = store_dir / f"{proposal_id}.json"
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return WikiProposal.from_dict(data)
    except Exception as e:
        logger.warning(f"Failed loading proposal {target}: {e}")
        return None
