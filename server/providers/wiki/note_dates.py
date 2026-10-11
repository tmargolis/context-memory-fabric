"""When a wiki note was created and last changed, and how each date is known.

One resolver shared by the graph scripts (seed/sweep/extract: the
created_at/updated_at MS7b's `Note` nodes carry), replay pruning and
`get_context`'s age display. Each date's source is reported here but not
stored in the graph.

**created_at**, first source that applies:

1. ``frontmatter``: the note's own ``created:`` field.
2. ``git``: the commit that first added the file, following renames (a
   delete-then-re-add counts from the re-add, so the date belongs to the file
   that exists now).
3. Files whose first commit is the repository's root commit (LLM_Wiki's
   2026-04-22 initial import of 705 files) only show that they existed on or
   before the import. ``disk-pre-import``: the file's disk birth time, when it
   is earlier than the import, which is real pre-git history.
   ``git-import-upper-bound``: otherwise the import time itself, an upper bound
   rather than an exact date (rule approved by the user, 2026-10-09).
4. ``disk``: the disk birth time, for files git has never seen.
5. ``filename``: overrides 2-4 when the file name carries a YYYY-MM-DD on an
   earlier day than they give (research notes named for the day they were
   written but committed later, e.g. during the 2026-08-31 -> 09-11 watcher
   outage). A later filename date (a plan for a future day) never wins, and
   frontmatter is never overridden (user, 2026-10-10).

**updated_at**: ``git``, the last commit that touched the file (exact since
Auto-sync resumed 2026-09-11), else ``frontmatter`` ``updated:``, else
``disk`` mtime.

A note with no file and no history gets ``None`` dates, never an invented
"now".

The git history is read once per repository with a single ``git log`` pass
(about a second for LLM_Wiki's ~14k commits) and then advanced incrementally
when HEAD moves, so per-note lookups make no git calls.
"""

from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

CREATED_SOURCES = ("frontmatter", "filename", "git", "disk-pre-import", "git-import-upper-bound", "disk")
NOTE_DATE_FIELDS = ("created_at", "updated_at")  # what a Note node stores

_FM_RE = {key: re.compile(rf"^{key}:\s*([^\n\r]+)", re.M) for key in ("created", "updated")}


def parse_date(value: Optional[str]) -> Optional[datetime]:
    """ISO date or datetime (``Z``, offset, or naive) -> aware datetime.
    Naive values are taken as UTC; unparseable values give None."""
    if not value:
        return None
    text = str(value).strip().strip("\"'")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_FILENAME_DATE = re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)")


def filename_date(rel_path: str) -> Optional[datetime]:
    """The first valid YYYY-MM-DD in a note's file name (not its folders), as UTC midnight."""
    for m in _FILENAME_DATE.finditer(Path(rel_path).name):
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def frontmatter_dates(text: str) -> dict[str, Optional[datetime]]:
    """``created:``/``updated:`` from a leading YAML frontmatter block."""
    out: dict[str, Optional[datetime]] = {"created": None, "updated": None}
    if not text.startswith("---"):
        return out
    end = text.find("\n---", 3)
    if end == -1:
        return out
    block = text[3:end]
    for key, rx in _FM_RE.items():
        m = rx.search(block)
        if m:
            out[key] = parse_date(m.group(1))
    return out


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


@dataclass
class GitDateIndex:
    """First-add and last-change commit times for every path in a repo's history."""

    repo: Path
    head: Optional[str] = None
    root_commit: Optional[str] = None
    root_date: Optional[datetime] = None
    first: dict[str, tuple[datetime, str]] = field(default_factory=dict)  # path -> (date, commit)
    last: dict[str, datetime] = field(default_factory=dict)

    @classmethod
    def build(cls, repo: Path) -> "GitDateIndex":
        index = cls(Path(repo))
        index.refresh()
        return index

    def refresh(self) -> None:
        """Bring the index up to the repo's current HEAD (no-op if unchanged)."""
        try:
            head = _git(self.repo, "rev-parse", "HEAD").strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return  # not a git repo, or no commits yet
        if head == self.head:
            return
        incremental = False
        if self.head:
            try:
                subprocess.run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor", self.head, head],
                               check=True, capture_output=True)
                incremental = True
            except subprocess.CalledProcessError:
                pass  # history rewritten: rebuild from scratch
        if not incremental:
            self.first, self.last = {}, {}
            roots = _git(self.repo, "rev-list", "--max-parents=0", head).split()
            # With several roots, the oldest one is the import.
            dated = sorted((parse_date(_git(self.repo, "show", "-s", "--format=%cI", r).strip()), r) for r in roots)
            self.root_date, self.root_commit = dated[0]
        rev_range = f"{self.head}..{head}" if incremental else head
        self._apply(_git(self.repo, "-c", "core.quotePath=false", "log", "-z", "--reverse", "-M",
                         "--name-status", "--format=%x01%H%x01%cI", rev_range))
        self.head = head

    def _apply(self, log: str) -> None:
        chunks = log.split("\x01")
        # ['', hash, "date\0\nSTATUS\0path\0...", hash, ...]
        for i in range(1, len(chunks) - 1, 2):
            commit, body = chunks[i], chunks[i + 1]
            fields = body.split("\0")
            when = parse_date(fields[0])
            entries = [f.lstrip("\n") for f in fields[1:]]
            j = 0
            while j < len(entries):
                status = entries[j]
                if not status:
                    j += 1
                    continue
                kind = status[0]
                if kind in ("R", "C"):
                    old, new = entries[j + 1], entries[j + 2]
                    j += 3
                    if kind == "R":
                        self.first[new] = self.first.pop(old, (when, commit))
                        self.last.pop(old, None)
                    else:
                        self.first[new] = (when, commit)
                    self.last[new] = when
                    continue
                path = entries[j + 1]
                j += 2
                if kind == "A":
                    self.first[path] = (when, commit)
                    self.last[path] = when
                elif kind == "D":
                    self.first.pop(path, None)
                    self.last.pop(path, None)
                else:  # M, T and anything else that changes content in place
                    self.first.setdefault(path, (when, commit))
                    self.last[path] = when


_INDEXES: dict[Path, GitDateIndex] = {}
_LOCK = threading.Lock()


def git_index(repo: Path) -> GitDateIndex:
    """The cached index for `repo`, advanced to its current HEAD."""
    key = Path(repo).resolve()
    with _LOCK:
        index = _INDEXES.get(key)
        if index is None:
            index = _INDEXES[key] = GitDateIndex(key)
        index.refresh()
        return index


def _disk_birth(st: Any) -> datetime:
    return datetime.fromtimestamp(getattr(st, "st_birthtime", st.st_ctime), tz=timezone.utc)


def resolve_note_dates(wiki_root: Path, rel_path: str, index: Optional[GitDateIndex] = None) -> dict[str, Any]:
    """Dates for one note. Keys: created_at / updated_at (ISO strings or None),
    created_dt / updated_dt (aware datetimes or None), created_source /
    updated_source (see module docstring; None when the date is unknown)."""
    wiki_root = Path(wiki_root)
    full = wiki_root / rel_path
    if index is None:
        index = git_index(wiki_root)

    fm: dict[str, Optional[datetime]] = {"created": None, "updated": None}
    st = None
    if full.is_file():
        st = full.stat()
        try:
            fm = frontmatter_dates(full.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass

    created, c_source = None, None
    if fm["created"]:
        created, c_source = fm["created"], "frontmatter"
    elif rel_path in index.first:
        added, commit = index.first[rel_path]
        if commit == index.root_commit:
            birth = _disk_birth(st) if st else None
            if birth and added and birth < added:
                created, c_source = birth, "disk-pre-import"
            else:
                created, c_source = added, "git-import-upper-bound"
        else:
            created, c_source = added, "git"
    elif st:
        created, c_source = _disk_birth(st), "disk"
    if c_source != "frontmatter":
        named = filename_date(rel_path)
        if named and (created is None or named.date() < created.date()):
            created, c_source = named, "filename"

    updated, u_source = None, None
    if rel_path in index.last:
        updated, u_source = index.last[rel_path], "git"
    elif fm["updated"]:
        updated, u_source = fm["updated"], "frontmatter"
    elif st:
        updated, u_source = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc), "disk"

    return {
        "created_at": created.isoformat() if created else None,
        "created_dt": created,
        "created_source": c_source,
        "updated_at": updated.isoformat() if updated else None,
        "updated_dt": updated,
        "updated_source": u_source,
    }


def note_date_row(wiki_root: Path, rel_path: str, index: Optional[GitDateIndex] = None) -> dict[str, Optional[str]]:
    """The date properties a `Note` node stores: created_at and updated_at as ISO
    strings. The sources stay out of the graph (user, 2026-10-10); resolve_note_dates
    still reports them."""
    d = resolve_note_dates(wiki_root, rel_path, index)
    return {k: d[k] for k in NOTE_DATE_FIELDS}


async def write_note_dates(driver: Any, rows: list[dict[str, Any]], *, batch_size: int = 500) -> int:
    """SET created_at/updated_at on existing `Note` nodes, matched by note_path.
    Each row: {"note_path", *NOTE_DATE_FIELDS}. Returns the number of nodes matched."""
    sets = ", ".join(f"n.{k} = row.{k}" for k in NOTE_DATE_FIELDS)
    matched = 0
    for start in range(0, len(rows), batch_size):
        res = await driver.execute_query(
            f"UNWIND $rows AS row MATCH (n:Note {{note_path: row.note_path}}) SET {sets} RETURN count(n) AS c",
            rows=rows[start:start + batch_size],
        )
        records = res[0] if isinstance(res, tuple) else res
        matched += (records[0]["c"] if records else 0) if isinstance(records, list) else 0
    return matched


def describe_age(dates: dict[str, Any], now: Optional[datetime] = None) -> Optional[str]:
    """One line for get_context, e.g. "created 2026-04-22 (on or before) | updated 2026-10-09 (1 day ago)"."""
    now = now or datetime.now(timezone.utc)
    parts = []
    created, updated = dates.get("created_dt"), dates.get("updated_dt")
    if created:
        note = " (on or before)" if dates.get("created_source") == "git-import-upper-bound" else ""
        parts.append(f"created {created.date().isoformat()}{note}")
    if updated:
        days = max(0, (now - updated).days)
        ago = "today" if days == 0 else ("1 day ago" if days == 1 else f"{days} days ago")
        parts.append(f"updated {updated.date().isoformat()} ({ago})")
    return " | ".join(parts) or None
