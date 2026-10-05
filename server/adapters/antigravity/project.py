"""Project tagging for Antigravity conversations.

Unlike server.adapters.claude_code.project_slug (a hardcoded path-encoding
heuristic, because Claude Code's transcript filenames are the only signal
available), Antigravity's own `conversation_summaries.db` -- one per
app_data_dir, alongside `brain/` -- already carries a clean `workspace_uris`
JSON array (file:// URIs) per conversation_id. We read that directly instead
of inventing a second heuristic.

Fallback (found 2026-09-24, reviewing the antigravity-ide "unknown" bucket
in docs/REVIEW-SESSION-NOTES.local.md): conversation_summaries.db is empty
on this machine for the `antigravity-ide` app_data_dir (0-byte file, no
`conversation_summaries` table at all), so the primary lookup above always
misses for that install. Rather than touching Antigravity's separate
proprietary per-conversation SQLite/protobuf store (~/.gemini/antigravity/
conversations/*.db -- explicitly ruled out, no parsing of undocumented
binary schemas), the conversation's own transcript.jsonl -- the same plain,
documented-shape file parser.py already reads -- already carries the answer:
tool-call arguments are full absolute paths, and the workspace they were
run in dominates every other path referenced by a wide margin (measured:
24-380 hits for the true workspace vs. a handful of incidental hits for
anything else, across all 10 affected conversations). `_project_from_
transcript_paths` extracts the most-referenced `/Users/<user>/{Dev,
Documents}/<project>/...` prefix as a second-choice signal, used only when
the primary conversation_summaries.db lookup comes back unknown.

Read-only access only: both this and conversation_summaries.db are written
by Antigravity itself, including while a session is live (WAL mode for the
db; the transcript is append-only) -- a read-only pass never blocks or is
blocked by that writer.
"""

from __future__ import annotations

from collections import Counter
import json
import re
import sqlite3
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

UNKNOWN_PROJECT = "unknown"

# Matches an absolute path under a "projects live here" root, capturing the
# next path segment as the candidate project slug. Deliberately narrow
# (Dev/Documents only, this machine's actual layout) rather than "any path
# under /Users/<user>/" -- a broad match would just as happily "win" on a
# heavily-referenced library path (site-packages, a venv) as on the real
# workspace.
_WORKSPACE_PATH_RE = re.compile(r"/Users/[^/\s\"]+/(?:Dev|Documents)/([A-Za-z0-9_.-]+)")

# A transcript is JSON lines of bounded-ish size (real ones seen: tens of KB
# to a few MB) -- cap the read defensively so a pathological/corrupt file
# can't make this scan itself a resource problem.
_TRANSCRIPT_SCAN_MAX_BYTES = 10 * 1024 * 1024

# Per-process cache: (app_data_dir, conversation_id) -> resolved project.
# The summaries db is only ever appended/updated by Antigravity, never by
# us, so a conversation's workspace_uris are effectively immutable for the
# lifetime of one worker pass -- caching avoids re-querying per event.
_cache: dict[tuple[str, str], str] = {}


def _first_workspace_slug(workspace_uris_json: Optional[str]) -> Optional[str]:
    if not workspace_uris_json:
        return None
    try:
        uris = json.loads(workspace_uris_json)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(uris, list) or not uris:
        return None
    first = uris[0]
    if not isinstance(first, str):
        return None
    path = urlparse(first).path
    slug = Path(path).name
    return slug or None


def _project_from_transcript_paths(conversation_id: str, app_data_dir: Path) -> Optional[str]:
    """Fallback: the most-referenced Dev/Documents project directory
    mentioned in this conversation's own transcript.jsonl. Never raises --
    a missing file, a read error, or no matches at all returns None.
    """
    transcript_path = app_data_dir / "brain" / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
    if not transcript_path.is_file():
        return None
    try:
        text = transcript_path.read_text(encoding="utf-8", errors="replace")[:_TRANSCRIPT_SCAN_MAX_BYTES]
    except OSError:
        return None

    matches = _WORKSPACE_PATH_RE.findall(text)
    if not matches:
        return None
    slug, _count = Counter(matches).most_common(1)[0]
    return slug


def resolve_project(conversation_id: str, app_data_dir: Path) -> str:
    """Best-effort project slug for `conversation_id`. Primary source is
    that app_data_dir's conversation_summaries.db; if that comes back
    unknown (missing db, missing row, malformed JSON, or -- the case this
    exists for -- an empty/uninitialized db file), falls back to scanning
    the conversation's own transcript.jsonl for its dominant workspace path
    (see module docstring). Never raises either way -- any failure in
    either path falls back to UNKNOWN_PROJECT rather than aborting the
    caller.
    """
    cache_key = (str(app_data_dir), conversation_id)
    if cache_key in _cache:
        return _cache[cache_key]

    resolved = UNKNOWN_PROJECT
    db_path = app_data_dir / "conversation_summaries.db"
    if db_path.is_file():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT workspace_uris FROM conversation_summaries WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
            finally:
                conn.close()
            if row is not None:
                slug = _first_workspace_slug(row[0])
                if slug:
                    resolved = slug
        except sqlite3.Error:
            pass

    if resolved == UNKNOWN_PROJECT:
        fallback = _project_from_transcript_paths(conversation_id, app_data_dir)
        if fallback:
            resolved = fallback

    _cache[cache_key] = resolved
    return resolved
