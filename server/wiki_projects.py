"""Wiki note -> project, from the note's `project:` frontmatter (B16).

The wiki file is the single source of truth (User, 2026-10-15): a note's
project is the `project: <slug>` line in its leading YAML frontmatter. Notes
without one have no project. The 2026-10-14 path/filename rules file that
first tagged notes was written into frontmatter on 2026-10-15 and retired.

Used by scripts/sweep_wiki_graph.py to set Note.project.
"""
from __future__ import annotations

from pathlib import Path
import re
from typing import Optional

_FRONTMATTER = re.compile(r"---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.S)
_PROJECT = re.compile(r"^project:[ \t]*([^\r\n]*)$", re.M)


def frontmatter_project(text: str) -> Optional[str]:
    """`project:` from a leading frontmatter block: a scalar slug, or a one-item
    inline list. None when absent, empty, or a list of several."""
    m = _FRONTMATTER.match(text.lstrip("﻿"))
    if not m:
        return None
    p = _PROJECT.search(m.group(1))
    if not p:
        return None
    value = p.group(1).split(" #", 1)[0].strip()
    if value.startswith("[") and value.endswith("]"):
        items = [i.strip() for i in value[1:-1].split(",") if i.strip()]
        value = items[0] if len(items) == 1 else ""
    value = value.strip("\"'").strip()
    return value or None


def project_for_file(wiki_root: Path, rel_path: str) -> Optional[str]:
    try:
        text = (Path(wiki_root) / rel_path.lstrip("/")).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return frontmatter_project(text)


async def refresh_note_projects(driver, wiki_root: Path, dry_run: bool) -> dict:
    """Set every :Note's `project` (a one-item list, like Entity.project) from
    its file's frontmatter; remove it where the file has none. Idempotent."""
    res = await driver.execute_query("MATCH (n:Note) RETURN n.note_path AS path, n.project AS project")
    recs = res[0] if res and isinstance(res[0], list) else (res or [])
    set_rows, clear = [], []
    tagged = 0
    for r in recs:
        want = project_for_file(wiki_root, r["path"] or "") if r["path"] else None
        tagged += want is not None
        have = r["project"]
        if want and have != [want]:
            set_rows.append({"path": r["path"], "project": [want]})
        elif not want and have is not None:
            clear.append(r["path"])
    if not dry_run:
        if set_rows:
            await driver.execute_query(
                "UNWIND $rows AS row MATCH (n:Note {note_path: row.path}) SET n.project = row.project", rows=set_rows)
        if clear:
            await driver.execute_query("UNWIND $paths AS p MATCH (n:Note {note_path: p}) REMOVE n.project", paths=clear)
    return {"notes": len(recs), "with_project": tagged, "set": len(set_rows), "cleared": len(clear), "dry_run": dry_run}
