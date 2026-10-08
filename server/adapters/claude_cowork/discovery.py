"""Find Cowork transcripts and their session metadata.

Claude Desktop's Cowork mode keeps each session in its own sandbox under
`~/Library/Application Support/Claude/local-agent-mode-sessions/<org>/<user>/`:

    local_<id>.json                                  sidecar: title, sessionType,
                                                     scheduledTaskId, userSelectedFolders,
                                                     outboundCCRRemoteId, createdAt, ...
    local_<id>/.claude/projects/<slug>/<uuid>.jsonl  the transcript, same JSONL format
                                                     as Claude Code (entrypoint "local-agent")

Most sessions have one transcript; a few have several (each its own
conversation, keyed by the JSONL's uuid like the Code adapter); 282
scheduled sessions (2026-10-02) have none. Nested `*/subagents/*.jsonl`
files are skipped, as in the Code adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_SESSIONS_ROOT = Path.home() / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"

# Session types whose conversations get ExtractPolicy consolidation by
# default. "scheduled" runs are journal-only unless their scheduledTaskId is
# opted in via CMF_COWORK_EXTRACT_SCHEDULED_TASKS (the user, 2026-10-02).
EXTRACT_SESSION_TYPES = frozenset({"interactive", "dispatch_child"})

# Sidecar fields copied onto every event's metadata (small, provenance-only;
# never the system prompt or tool snapshots, which run to 100s of KB).
SIDECAR_FIELDS = ("title", "sessionType", "scheduledTaskId", "outboundCCRRemoteId",
                  "dispatchParentOrigin", "parentSessionId", "model")


@dataclass
class CoworkTranscript:
    path: Path
    session_id: str                 # transcript uuid (JSONL stem) -> conversation_id
    cowork_session_id: str          # local_<id>
    session_type: str               # interactive | scheduled | dispatch_child
    project: Optional[str]
    project_folder: Optional[str]
    created_at_ms: Optional[int]
    sidecar: dict[str, Any] = field(default_factory=dict)

    @property
    def tail_key(self) -> str:
        return str(self.path)

    def extract_eligible(self, scheduled_allowlist: frozenset[str]) -> bool:
        if self.session_type in EXTRACT_SESSION_TYPES:
            return True
        return self.session_type == "scheduled" and self.sidecar.get("scheduledTaskId") in scheduled_allowlist


def scheduled_allowlist() -> frozenset[str]:
    raw = os.getenv("CMF_COWORK_EXTRACT_SCHEDULED_TASKS") or ""
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def scheduled_task_projects() -> dict[str, str]:
    """`CMF_COWORK_SCHEDULED_TASK_PROJECTS="task=project,..."` (the user, 2026-10-03).

    Scheduled tasks run without a selected folder, so without this their
    conversations carry no project and promote untagged. Malformed entries
    are skipped with a warning.
    """
    out: dict[str, str] = {}
    for item in (os.getenv("CMF_COWORK_SCHEDULED_TASK_PROJECTS") or "").split(","):
        if not item.strip():
            continue
        task, sep, project = item.partition("=")
        if not sep or not task.strip() or not _slug(project):
            logger.warning("cowork: ignoring malformed CMF_COWORK_SCHEDULED_TASK_PROJECTS entry %r", item)
            continue
        out[task.strip()] = _slug(project)
    return out


def folder_project_map() -> list[tuple[str, str]]:
    """`CMF_PROJECT_FOLDER_MAP="path=project,..."` (2026-10-03): a folder (and
    everything under it) belongs to the named project. `~` expands; matching
    is case-insensitive and the longest prefix wins, so a data folder nested
    inside a repo can belong to a different project than the repo itself.
    """
    out: list[tuple[str, str]] = []
    for item in (os.getenv("CMF_PROJECT_FOLDER_MAP") or "").split(","):
        if not item.strip():
            continue
        path, sep, project = item.partition("=")
        path = os.path.expanduser(path.strip()).rstrip("/")
        if not sep or not path or not _slug(project):
            logger.warning("cowork: ignoring malformed CMF_PROJECT_FOLDER_MAP entry %r", item)
            continue
        out.append((path.lower(), _slug(project)))
    return sorted(out, key=lambda pp: len(pp[0]), reverse=True)


def project_for_mapped_folder(folder: Optional[str], folder_map: list[tuple[str, str]]) -> Optional[str]:
    if not folder:
        return None
    f = folder.rstrip("/").lower()
    for prefix, project in folder_map:
        if f == prefix or f.startswith(prefix + "/"):
            return project
    return None


def project_for_session(
    folder: Optional[str],
    sidecar: dict[str, Any],
    task_projects: dict[str, str],
    folder_map: Optional[list[tuple[str, str]]] = None,
) -> Optional[str]:
    """Scheduled-task mapping first, then the folder map, then the selected
    folder's own name, else None.

    The mapping is an explicit per-task decision, so it beats the folder
    heuristic: some scheduled tasks do carry a folder, and it can name a
    different project than the one the task belongs to. Dispatch children don't inherit from their parent: every one seen
    (2026-10-03) hangs off the single Dispatch orchestrator session, which
    has no sidecar or folder and spans every topic.
    """
    return (
        task_projects.get(sidecar.get("scheduledTaskId") or "")
        or project_for_mapped_folder(folder, folder_map if folder_map is not None else folder_project_map())
        or project_for_folder(folder)
    )


def project_for_journaled_session(metadata: dict[str, Any]) -> Optional[str]:
    """A journaled Cowork event's project, re-derived from its folder and sidecar.

    Events keep the metadata they were journaled with, so a folder or
    scheduled-task mapping added later never reaches `metadata["project"]`.
    Re-deriving here lets extraction from an existing journal (a rebuild, a
    new policy version) see the current maps. The stored value is the
    fallback when nothing derives.
    """
    derived = project_for_session(
        metadata.get("project_folder"), metadata.get("cowork") or {}, scheduled_task_projects()
    )
    return derived or metadata.get("project")


_HOME = str(Path.home())


def project_for_folder(folder: Optional[str]) -> Optional[str]:
    """Project slug for a Cowork session's first selected folder.

    `~/Dev/<p>/...` and `~/Documents/<p>/...` -> `<p>` (matching the Code
    adapter's ~/Dev convention), except Claude Desktop's own project folders,
    `~/Documents/Claude/Projects/<p>/...` -> `<p>` (found 2026-10-03: every
    one of them had collapsed to `claude`); anything else -> the folder's own
    slugified name, e.g. `~/Notes_Vault` -> `notes-vault`. The transcript's own project dir is a
    sandbox path (`-sessions-<name>`), useless for this.
    """
    if not folder:
        return None
    path = folder.rstrip("/")
    for root in ("Documents/Claude/Projects", "Dev", "Documents"):
        prefix = f"{_HOME}/{root}/"
        if path.lower().startswith(prefix.lower()):
            rest = path[len(prefix):].split("/", 1)[0]
            return _slug(rest) if rest else None
    return _slug(Path(path).name)


def _slug(value: str) -> Optional[str]:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return s or None


def _load_sidecar(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("cowork: unreadable sidecar %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def discover_transcripts(sessions_root: Optional[Path] = None) -> list[CoworkTranscript]:
    root = sessions_root or DEFAULT_SESSIONS_ROOT
    if not root.is_dir():
        return []
    out: list[CoworkTranscript] = []
    task_projects = scheduled_task_projects()
    folder_map = folder_project_map()
    for sidecar_path in sorted(root.glob("*/*/local_*.json")):
        sidecar = _load_sidecar(sidecar_path)
        if sidecar is None:
            continue
        session_dir = sidecar_path.with_suffix("")
        folders = sidecar.get("userSelectedFolders") or []
        folder = folders[0] if folders and isinstance(folders[0], str) else None
        kept = {k: sidecar[k] for k in SIDECAR_FIELDS if sidecar.get(k) not in (None, "", [], {})}
        for jsonl in sorted(session_dir.glob(".claude/projects/*/*.jsonl")):
            out.append(CoworkTranscript(
                path=jsonl,
                session_id=jsonl.stem,
                cowork_session_id=sidecar.get("sessionId") or session_dir.name,
                session_type=sidecar.get("sessionType") or "interactive",
                project=project_for_session(folder, sidecar, task_projects, folder_map),
                project_folder=folder,
                created_at_ms=sidecar.get("createdAt"),
                sidecar=kept,
            ))
    return out
