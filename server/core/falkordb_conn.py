"""FalkorDB connection settings, from the environment, for every client CMF opens.

FALKORDB_HOST (default localhost), FALKORDB_PORT (6379), FALKORDB_USER
(alias FALKORDB_USERNAME) and FALKORDB_PASSWORD. With no user set, clients
connect as FalkorDB's `default` user, as before. Set a user once that
deployment turns `default` off (ACL SETUSER default off), as this one did on
2026-10-10; then every client must authenticate, including scripts and replay.

Kept free of Graphiti imports so one-off scripts can use it cheaply.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def connection_params() -> dict[str, Any]:
    """host, port, username, password -- accepted by redis.Redis, falkordb.FalkorDB
    and graphiti's FalkorDriver alike."""
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV_FILE, override=False)  # never clobbers values already set (tests, launchd)
    except ImportError:
        pass
    return {
        "host": os.getenv("FALKORDB_HOST", "localhost"),
        "port": int(os.getenv("FALKORDB_PORT", "6379")),
        "username": os.getenv("FALKORDB_USER") or os.getenv("FALKORDB_USERNAME") or None,
        "password": os.getenv("FALKORDB_PASSWORD") or None,
    }


def redis_client(**kwargs: Any):
    """A redis.Redis connected with CMF's FalkorDB credentials (for GRAPH.* commands)."""
    import redis

    return redis.Redis(**connection_params(), **kwargs)


def falkordb_client(**kwargs: Any):
    """A falkordb.FalkorDB connected with CMF's FalkorDB credentials."""
    from falkordb import FalkorDB

    return FalkorDB(**connection_params(), **kwargs)
