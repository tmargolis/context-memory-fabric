"""Where CMF's configuration and state live: one .env, read once.

CMF used to call load_dotenv() in about ten places, each searching for the
repo's .env from its own module. Two installs on one machine then read
different settings from different files (docs/setup-simplification-plan.md,
P1), and nothing said which file a process had used. Now there is one rule:

1. `$CMF_HOME/.env` (CMF_HOME defaults to ~/.cmf): the consolidated install,
   used by both Standard and Developer mode (plan section 3.8).
2. Otherwise the repo checkout's .env: the older developer layout.

When both exist, ~/.cmf wins and a warning names the ignored repo file.
Values already in the real environment (compose, launchd, an MCP client's
`env` block, tests) always win over either file.

Only a ~/.cmf install moves state: there `CMF_STATE_DIR` defaults to
`$CMF_HOME/state`. A repo-.env install keeps state in the repo, as before.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_ENV_FILE = REPO_ROOT / ".env"

_loaded_from: Optional[Path] = None
_loaded = False


def cmf_home() -> Path:
    return Path(os.getenv("CMF_HOME") or Path.home() / ".cmf").expanduser()


def home_env_file() -> Path:
    return cmf_home() / ".env"


def resolve_env_file() -> Optional[Path]:
    """The .env this process should read, or None if there is none."""
    home_env = home_env_file()
    if home_env.is_file():
        if REPO_ENV_FILE.is_file() and REPO_ENV_FILE.resolve() != home_env.resolve():
            logger.warning(
                f"Using {home_env}; ignoring {REPO_ENV_FILE}. Two config files can disagree "
                "(rename the repo one once ~/.cmf is set up)."
            )
        return home_env
    if REPO_ENV_FILE.is_file():
        return REPO_ENV_FILE
    return None


def load_env(force: bool = False) -> Optional[Path]:
    """Load the CMF .env once per process; return the file used, if any.

    `force` re-reads the file (still never overriding values already set).
    """
    global _loaded, _loaded_from
    if _loaded and not force:
        return _loaded_from
    from dotenv import load_dotenv

    path = resolve_env_file()
    if path is not None:
        load_dotenv(path, override=False)
        if path == home_env_file():
            os.environ.setdefault("CMF_STATE_DIR", str(cmf_home() / "state"))
    _loaded, _loaded_from = True, path
    return path


def loaded_env_file() -> Optional[Path]:
    return _loaded_from


def state_dir() -> Path:
    """CMF_STATE_DIR when set (~/.cmf/state on the host, /data/cmf in a container), else the repo."""
    state = os.getenv("CMF_STATE_DIR")
    return Path(state).expanduser().resolve() if state else REPO_ROOT


def imports_dir() -> Path:
    """journal, import registry and other imports/ state: under CMF_STATE_DIR when set."""
    return state_dir() / "imports"


def in_container() -> bool:
    return Path("/.dockerenv").exists()


# The Docker stack keeps imports/journal (the SQLite journal and its lock
# files) in the cmf-journal volume, not on the ~/.cmf/state bind mount:
# SQLite fails when a host process and a container open the same file
# through Docker Desktop's file sharing ("disk I/O error"). The containers
# leave this marker in imports/ so host-side tools refuse instead of quietly
# starting an empty journal in ~/.cmf/state.
JOURNAL_VOLUME_MARKER = "journal.in-docker-volume"


class JournalInDockerVolume(RuntimeError):
    pass


def journal_dir() -> Path:
    """imports/journal: the journal database and its lock files."""
    marker = imports_dir() / JOURNAL_VOLUME_MARKER
    if in_container():
        if os.getenv("CMF_JOURNAL_VOLUME"):
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(f"The journal is in the Docker volume {os.environ['CMF_JOURNAL_VOLUME']}.\n")
            except OSError:
                pass
    elif marker.exists():
        raise JournalInDockerVolume(
            f"The CMF journal lives in the Docker volume (see {marker}), not in {imports_dir() / 'journal'}. "
            "Run journal tools inside the stack, e.g. "
            "`cd ~/.cmf && docker compose exec cmf-server python -m server.journal.cli stats`."
        )
    return imports_dir() / "journal"


def review_rules_path() -> Path:
    """The operator's own review rules: CMF_REVIEW_RULES_PATH, else <state>/review-rules.md.

    The default sits in the state folder so the host and the containers (which
    mount it at /data/cmf) read the same file. A relative CMF_REVIEW_RULES_PATH
    counts from the state folder too.
    """
    value = (os.getenv("CMF_REVIEW_RULES_PATH") or "").strip()
    if not value:
        return state_dir() / "review-rules.md"
    path = Path(value).expanduser()
    return path if path.is_absolute() else state_dir() / path
