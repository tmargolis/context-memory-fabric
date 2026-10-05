"""Derives a project slug for claude_code/claude_desktop harness rows from
the transcript's own `project_path` metadata (a directory-based ground
truth) -- not server.review.projects's keyword taxonomy, which classifies
statement/reason text and was never built with coding-session project
directories in mind (see docs/plan-active.md, MS4b Backlog: "Extend MS4b's
`project_path` is `parser.py`'s encoding of the transcript's containing
directory with '/' replaced by '-' (e.g. "-Users-<user>-Dev-jspace").
"""

from __future__ import annotations

import re

# Matches standard local project root encodings like -Users-<user>-Dev-<project>
# or -Volumes-<volume>-data-projects-<project>
_PROJECT_ROOT_RE = re.compile(
    r"^-(?:Users-[^-]+-(?:Dev|Documents)|Volumes-[^-]+-(?:data-)?projects)-"
)


def derive_project_from_path(project_path: str | None) -> str:
    if not project_path:
        return "unknown"
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
