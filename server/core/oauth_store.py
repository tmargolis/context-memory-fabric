"""Persistent storage for the CMF OAuth 2.1 authorization server (MS6c).

Registered clients, access tokens, and refresh tokens live in the same
sqlite file as the journal/consolidation state -- see
server.consolidation.promotion.PromotionStore's module docstring for the
"one file" precedent this follows. They're operational server state of the
same kind, not episodic memory.

Authorization codes and pending consent requests deliberately do NOT live
here -- see server.core.oauth_provider's module docstring for why losing
them on a server restart is correct behavior, not a durability gap.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from server.journal.store import DEFAULT_JOURNAL_PATH

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_info_json TEXT NOT NULL,
    registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS oauth_access_tokens (
    token TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    expires_at REAL,
    resource TEXT
);
CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
    token TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    expires_at REAL
);
CREATE INDEX IF NOT EXISTS idx_oauth_access_tokens_client ON oauth_access_tokens(client_id);
CREATE INDEX IF NOT EXISTS idx_oauth_refresh_tokens_client ON oauth_refresh_tokens(client_id);
"""


class OAuthStore:
    """Sqlite-backed persistence for DCR-registered clients and issued tokens."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_JOURNAL_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- clients ------------------------------------------------------

    def save_client(self, client_id: str, client_info_json: str) -> None:
        self._conn.execute(
            """
            INSERT INTO oauth_clients (client_id, client_info_json)
            VALUES (?, ?)
            ON CONFLICT(client_id) DO UPDATE SET client_info_json = excluded.client_info_json
            """,
            (client_id, client_info_json),
        )
        self._conn.commit()

    def get_client(self, client_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT client_info_json FROM oauth_clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        return row["client_info_json"] if row else None

    # -- access tokens --------------------------------------------------

    def save_access_token(
        self,
        token: str,
        client_id: str,
        scopes_json: str,
        expires_at: Optional[float],
        resource: Optional[str],
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO oauth_access_tokens "
            "(token, client_id, scopes_json, expires_at, resource) VALUES (?, ?, ?, ?, ?)",
            (token, client_id, scopes_json, expires_at, resource),
        )
        self._conn.commit()

    def get_access_token(self, token: str) -> Optional[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM oauth_access_tokens WHERE token = ?", (token,)).fetchone()

    def delete_access_token(self, token: str) -> None:
        self._conn.execute("DELETE FROM oauth_access_tokens WHERE token = ?", (token,))
        self._conn.commit()

    # -- refresh tokens ---------------------------------------------------

    def save_refresh_token(
        self, token: str, client_id: str, scopes_json: str, expires_at: Optional[float]
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO oauth_refresh_tokens "
            "(token, client_id, scopes_json, expires_at) VALUES (?, ?, ?, ?)",
            (token, client_id, scopes_json, expires_at),
        )
        self._conn.commit()

    def get_refresh_token(self, token: str) -> Optional[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM oauth_refresh_tokens WHERE token = ?", (token,)).fetchone()

    def delete_refresh_token(self, token: str) -> None:
        self._conn.execute("DELETE FROM oauth_refresh_tokens WHERE token = ?", (token,))
        self._conn.commit()
