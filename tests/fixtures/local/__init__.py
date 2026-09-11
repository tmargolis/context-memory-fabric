"""Loader for gitignored, personal test fixtures.

A handful of tests are motivated by (or reproduce) a real personal example —
not synthetic data, unlike everything else under tests/fixtures/. Those
examples live in *.local.json files here, gitignored, read only through
`load()`. When the file is absent (a fresh clone, CI, another machine), the
test that needs it calls `load()`, gets None, and skips itself rather than
failing — the same treatment `taxonomy.local.json` already gets elsewhere.

Every test that depends on this data must have a synthetic, always-runs
sibling test proving the same behavior — this directory is for preserving a
real historical example, not for coverage that only exists on one machine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

_DIR = Path(__file__).parent


def load(name: str) -> Optional[dict[str, Any]]:
    """Return the parsed contents of `<name>.local.json`, or None if absent."""
    path = _DIR / f"{name}.local.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())
