"""Where projects live on disk: CMF_PROJECT_ROOTS and CMF_PROJECT_FOLDER_MAP.

Each transcript adapter turns a working directory into a project slug: the
first folder under a "projects live here" root. Unset, every adapter keeps
its own built-in pattern (this deployment's `~/Dev`, `~/Documents` and
`/Volumes/<v>/projects` layout), so existing slugs don't move.
`CMF_PROJECT_ROOTS="~/code,~/work"` replaces those patterns with the listed
folders.

`CMF_PROJECT_FOLDER_MAP="path=project,..."` names the project of one folder
and everything under it, ahead of the root rule; the longest prefix wins,
so a data folder nested inside a repo can belong to another project.

Paths are compared case-insensitively with `/` and `\\` treated alike, so a
Windows path (`C:\\Users\\x\\Dev\\proj`) matches a root written either way.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# A project folder name, as the built-in patterns have always captured it.
_SLUG_SEGMENT = r"([A-Za-z0-9_.-]+)"


def _slug(value: str) -> Optional[str]:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return s or None


def _norm_path(path: str) -> str:
    # Codex records some cwds as file:// URLs.
    path = re.sub(r"^file:(?://)?", "", path.strip())
    return os.path.expanduser(path).replace("\\", "/").rstrip("/")


def encode_claude_path(path: str) -> str:
    """A path in Claude Code's `~/.claude/projects/<folder>` form: every
    character other than a letter or digit becomes `-`
    (`/Users/x/Dev/proj` -> `-Users-x-Dev-proj`, `C:\\x` -> `C--x`)."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def project_roots() -> Optional[list[str]]:
    """The CMF_PROJECT_ROOTS folders, longest first, or None when unset."""
    roots = [_norm_path(item) for item in (os.getenv("CMF_PROJECT_ROOTS") or "").split(",")]
    roots = [r for r in roots if r]
    if not roots:
        return None
    return sorted(set(roots), key=len, reverse=True)


def root_path_regex(roots: list[str]) -> re.Pattern:
    """Matches `<root>/<project>` anywhere in a string, capturing the project
    folder. Either separator matches, so a Windows path need not be
    normalised first."""
    alternation = "|".join(re.escape(r).replace("/", r"[/\\]") for r in roots)
    return re.compile(rf"(?:{alternation})[/\\]{_SLUG_SEGMENT}", re.IGNORECASE)


def claude_root_prefixes(roots: list[str]) -> list[str]:
    """Each root in Claude Code's encoded form, lowercased, with the
    trailing `-` that separates it from the project folder."""
    return sorted((encode_claude_path(r).lower() + "-" for r in roots), key=len, reverse=True)


def folder_project_map() -> list[tuple[str, str]]:
    """CMF_PROJECT_FOLDER_MAP as (lowercased folder, project slug) pairs,
    longest folder first. Malformed entries are skipped with a warning."""
    out: list[tuple[str, str]] = []
    for item in (os.getenv("CMF_PROJECT_FOLDER_MAP") or "").split(","):
        if not item.strip():
            continue
        path, sep, project = item.partition("=")
        path = _norm_path(path)
        slug = _slug(project)
        if not sep or not path or not slug:
            logger.warning("ignoring malformed CMF_PROJECT_FOLDER_MAP entry %r", item)
            continue
        out.append((path.lower(), slug))
    return sorted(out, key=lambda pp: len(pp[0]), reverse=True)


def project_for_mapped_folder(
    folder: Optional[str], folder_map: Optional[list[tuple[str, str]]] = None
) -> Optional[str]:
    """The mapped project of `folder` (or a folder above it), else None."""
    if not folder:
        return None
    table = folder_project_map() if folder_map is None else folder_map
    f = _norm_path(folder).lower()
    for prefix, project in table:
        if f == prefix or f.startswith(prefix + "/"):
            return project
    return None


def project_for_encoded_folder(
    encoded: Optional[str], folder_map: Optional[list[tuple[str, str]]] = None
) -> Optional[str]:
    """The folder map applied to a Claude Code encoded folder name.

    The encoding is lossy (`/`, `-` and `.` all become `-`), so a sibling
    whose name extends a mapped folder's (`data-old` next to `data`) matches
    too. Map the sibling explicitly if that matters.
    """
    if not encoded:
        return None
    table = folder_project_map() if folder_map is None else folder_map
    e = encoded.lower()
    for prefix, project in table:
        enc = encode_claude_path(prefix).lower()
        if e == enc or e.startswith(enc + "-"):
            return project
    return None
