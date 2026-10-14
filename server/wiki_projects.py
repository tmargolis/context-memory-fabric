"""Wiki path -> project, from a local rules file (B16, 2026-10-14).

The wiki has no project metadata, and the entities a note mentions are too
noisy to infer one (generic entities pull job-role notes toward `finance`).
Folder and filename conventions are reliable, so a short ordered list of glob
rules is the source of truth. It lives outside the repo because it names
specific wiki files:

    CMF_WIKI_PROJECT_RULES_FILE  (default imports/state/wiki_project_rules.txt)

Format: `<glob> = <project>` per line, `#` comments, first match wins, `*`
matches across folders (fnmatch). Unmatched paths have no project. Used by
scripts/tag_note_projects.py (Note.project) and the wiki-side project boost.
"""
from __future__ import annotations

from fnmatch import fnmatchcase
import os
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT = _ROOT / "imports" / "state" / "wiki_project_rules.txt"
_cache: dict[str, tuple[float, list[tuple[str, str]]]] = {}


def rules_path() -> Path:
    return Path(os.environ.get("CMF_WIKI_PROJECT_RULES_FILE") or _DEFAULT).expanduser()


def parse_rules(text: str) -> list[tuple[str, str]]:
    rules = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        glob, sep, project = line.rpartition("=")
        if not sep or not glob.strip() or not project.strip():
            raise ValueError(f"wiki project rules line {n}: expected '<glob> = <project>'")
        rules.append((glob.strip(), project.strip()))
    return rules


def load_rules(path: Optional[Path] = None) -> list[tuple[str, str]]:
    """Rules from `path` (default rules_path()), re-read when the file changes; [] if absent."""
    path = path or rules_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    key = str(path)
    if key not in _cache or _cache[key][0] != mtime:
        _cache[key] = (mtime, parse_rules(path.read_text(encoding="utf-8")))
    return _cache[key][1]


def project_for_path(rel_path: str, rules: Optional[list[tuple[str, str]]] = None) -> Optional[str]:
    rel_path = rel_path.lstrip("/")
    for glob, project in rules if rules is not None else load_rules():
        if fnmatchcase(rel_path, glob):
            return project
    return None


async def refresh_note_projects(driver, dry_run: bool) -> dict:
    """Set every :Note's `project` (a one-item list, like Entity.project) from
    the rules; remove it where no rule matches. Idempotent. Run by
    scripts/sweep_wiki_graph.py, so new or moved notes are tagged too."""
    rules = load_rules()
    if not rules:
        return {"skipped": f"no rules file at {rules_path()}"}
    res = await driver.execute_query("MATCH (n:Note) RETURN n.note_path AS path, n.project AS project")
    recs = res[0] if res and isinstance(res[0], list) else (res or [])
    set_rows, clear = [], []
    tagged = 0
    for r in recs:
        want = project_for_path(r["path"] or "", rules)
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
