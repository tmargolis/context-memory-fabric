"""Project-slug aliases applied wherever a source project is resolved.

Two slugs can name one undertaking -- a repo renamed, or a sibling folder
that belongs to the same project. `CMF_PROJECT_ALIASES="old=new,..."` folds
the old slug into the new one at extraction time, so review buckets and the
promotion-time graph tags see a single project. Kept in `.env` (local, not
tracked) because the slugs are the operator's own project names.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def project_aliases() -> dict[str, str]:
    out: dict[str, str] = {}
    for item in (os.getenv("CMF_PROJECT_ALIASES") or "").split(","):
        if not item.strip():
            continue
        old, sep, new = item.partition("=")
        old, new = _slug(old), _slug(new)
        if not sep or not old or not new or old == new:
            logger.warning("ignoring malformed CMF_PROJECT_ALIASES entry %r", item)
            continue
        out[old] = new
    return out


def resolve_project(project: Optional[str], aliases: Optional[dict[str, str]] = None) -> Optional[str]:
    """`project` with its alias applied (one hop; aliases are not chained)."""
    if not project:
        return project
    table = project_aliases() if aliases is None else aliases
    return table.get(project, project)
