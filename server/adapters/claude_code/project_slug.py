"""Derives a project slug for claude_code/claude_desktop harness rows from
the transcript's own `project_path` metadata (a directory-based ground
truth) -- not server.review.projects's keyword taxonomy, which classifies
statement/reason text and was never built with coding-session project
directories in mind (see docs/plan-active.md, MS4b Backlog: "Extend MS4b's
`project_path` is `parser.py`'s encoding of the transcript's containing
directory with '/' replaced by '-' (e.g. "-Users-<user>-Dev-jspace").
CMF_PROJECT_FOLDER_MAP is applied first, then the project roots
(CMF_PROJECT_ROOTS, or the built-in pattern when unset); see
server.core.project_roots.
"""

from __future__ import annotations

import re

from server.core.project_roots import claude_root_prefixes, project_for_encoded_folder, project_roots

# Built-in roots when CMF_PROJECT_ROOTS is unset: encodings like
# -Users-<user>-Dev-<project> or -Volumes-<volume>-data-projects-<project>
_PROJECT_ROOT_RE = re.compile(
    r"^-(?:Users-[^-]+-(?:Dev|Documents)|Volumes-[^-]+-(?:data-)?projects)-"
)


def derive_project_from_path(project_path: str | None) -> str:
    if not project_path:
        return "unknown"
    mapped = project_for_encoded_folder(project_path)
    if mapped:
        return mapped
    roots = project_roots()
    if roots is not None:
        lowered = project_path.lower()
        for prefix in claude_root_prefixes(roots):
            if lowered.startswith(prefix) and len(project_path) > len(prefix):
                return project_path[len(prefix):]
    else:
        m = _PROJECT_ROOT_RE.match(project_path)
        if m:
            return project_path[m.end():]
    if project_path.startswith("-private-var-folders-"):
        tail = project_path.rsplit("-T-", 1)[-1]
        if tail.startswith("ms7-answer-eval") or tail.startswith("tmp"):
            # CMF's own eval/test fixtures (tests/fixtures/ms7_eval/), not
            # an external project.
            return "context-memory-fabric"
        return "tmp-other"
    return "other"
