"""Project resolution for Codex conversations.

Derives a project slug from the session/workspace `cwd` and `workspace_roots`
following project slug conventions (e.g. `Dev/<project>`,
`Documents/<project>`, `/Volumes/<volume>/projects/<project>`).

Per docs/CODEX-CAPTURE-PLAN.md:
"Projectless sessions remain explicitly unassigned rather than inheriting
the current repository."
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Optional, Sequence

# Matches a project directory under standard local project roots
_PROJECT_PATH_RE = re.compile(
    r"(?:/Users/[^/\s\"]+/(?:Dev|Documents)|/Volumes/[^/\s\"]+/(?:data/)?projects)/([A-Za-z0-9_.-]+)"
)


def project_from_path(path_str: Optional[str]) -> Optional[str]:
    """Derive project slug from a filesystem path, or None if projectless.

    Handles normal checkouts and git worktrees (e.g. stripping .worktrees/...).
    """
    if not path_str:
        return None

    norm = os.path.normpath(str(path_str).strip())
    # Exclude root, user home, generic temp directories
    if norm in ("/", str(Path.home()), "/tmp", "/private/tmp", "/var", "/private/var"):
        return None

    m = _PROJECT_PATH_RE.search(norm)
    if not m:
        return None

    raw_slug = m.group(1)
    # Worktree naming convention: if slug contains worktree or is inside a worktrees dir
    if raw_slug in ("worktrees", ".worktrees"):
        parts = norm.split("/")
        try:
            idx = parts.index(raw_slug)
            if idx + 1 < len(parts):
                return parts[idx + 1]
        except ValueError:
            pass

    return raw_slug


def resolve_project(
    cwd: Optional[str],
    workspace_roots: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """Resolve project slug from session metadata (cwd and workspace roots).

    Returns None for projectless sessions (e.g. session opened in $HOME or /tmp).
    """
    if cwd:
        proj = project_from_path(cwd)
        if proj:
            return proj

    if workspace_roots:
        for root in workspace_roots:
            proj = project_from_path(root)
            if proj:
                return proj

    return None
