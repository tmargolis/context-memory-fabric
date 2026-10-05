"""The local Gmail snapshot format the Gmail knowledge provider reads.

A snapshot is a directory:

    <dir>/manifest.json          {"account": "...", "query": "...", "created_at": "..."}
    <dir>/messages/<id>.json     one message per file (MailMessage fields)

CMF holds no Gmail credentials, so something else fills the snapshot: the
Takeout importer (scripts/import_gmail_mbox.py), or an assistant session
with a Gmail connector. Keeping the provider on a local, read-only snapshot
also means a search never reaches the network and never changes the mailbox.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Optional

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass
class MailMessage:
    id: str
    thread_id: str
    date: str  # ISO 8601 with offset
    subject: str
    body: str
    sender: str = ""
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    @property
    def timestamp(self) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(self.date)
        except ValueError:
            return None


def write_snapshot(directory: Path, messages: list[MailMessage], manifest: dict[str, Any]) -> int:
    """Write (or add to) a snapshot. Returns how many message files were written."""
    msg_dir = Path(directory) / "messages"
    msg_dir.mkdir(parents=True, exist_ok=True)
    (Path(directory) / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for m in messages:
        if not _SAFE_ID.match(m.id):
            raise ValueError(f"unsafe message id {m.id!r}")
        (msg_dir / f"{m.id}.json").write_text(json.dumps(asdict(m), ensure_ascii=False, indent=1) + "\n")
    return len(messages)


def read_snapshot(directory: Path) -> tuple[dict[str, Any], list[MailMessage]]:
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    messages = [MailMessage(**json.loads(p.read_text())) for p in sorted((directory / "messages").glob("*.json"))]
    return manifest, messages
