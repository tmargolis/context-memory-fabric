"""Persistent storage for the CMF OAuth 2.1 authorization server (MS6c).

Registered clients, access tokens, and refresh tokens live in the same
sqlite file as the journal/consolidation state -- see
server.consolidation.promotion.PromotionStore's module docstring for the
"one file" precedent this follows. They're operational server state of the
same kind, not episodic memory.

Authorization codes and pending consent requests deliberately do NOT live
here -- see server.core.oauth_provider's module docstring for why losing
them on a server restart is correct behavior, not a durability gap.

Tokens are stored as SHA-256 hex digests, never as the raw bearer value, so
a stray copy or backup of journal.db isn't a set of working credentials.
Callers always pass and receive raw tokens; hashing happens only here. The
tokens are 256-bit random values, so an unsalted fast hash is sufficient.
Rows written before hashing (raw 43-char values) are migrated two ways: the
server calls `hash_legacy_tokens()` once at startup, and a lookup that hits
a legacy row rehashes it on the spot. The bulk pass is deliberately not run
from __init__: tests import server.mcp (and so construct a store on the real
journal.db), and a bulk rehash there would lock out a still-running server
built before this change.
"""

from __future__ import annotations

import hashlib
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


_TOKEN_TABLES = ("oauth_access_tokens", "oauth_refresh_tokens")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_hashed(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


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

    def hash_legacy_tokens(self) -> int:
        """Replace every raw (pre-hashing) token row with its hash. Idempotent;
        returns the number of rows migrated. Called once at server startup."""
        migrated = 0
        for table in _TOKEN_TABLES:
            raw = [r["token"] for r in self._conn.execute(f"SELECT token FROM {table}") if not _is_hashed(r["token"])]
            for token in raw:
                self._conn.execute(f"UPDATE OR REPLACE {table} SET token = ? WHERE token = ?", (hash_token(token), token))
            migrated += len(raw)
        self._conn.commit()
        return migrated

    def _get_token_row(self, table: str, token: str) -> Optional[sqlite3.Row]:
        digest = hash_token(token)
        row = self._conn.execute(f"SELECT * FROM {table} WHERE token = ?", (digest,)).fetchone()
        if row is not None:
            return row
        legacy = self._conn.execute(f"SELECT * FROM {table} WHERE token = ?", (token,)).fetchone()
        if legacy is None:
            return None
        self._conn.execute(f"UPDATE OR REPLACE {table} SET token = ? WHERE token = ?", (digest, token))
        self._conn.commit()
        return self._conn.execute(f"SELECT * FROM {table} WHERE token = ?", (digest,)).fetchone()

    def _delete_token(self, table: str, token: str) -> None:
        self._conn.execute(f"DELETE FROM {table} WHERE token IN (?, ?)", (hash_token(token), token))
        self._conn.commit()

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
            (hash_token(token), client_id, scopes_json, expires_at, resource),
        )
        self._conn.commit()

    def get_access_token(self, token: str) -> Optional[sqlite3.Row]:
        return self._get_token_row("oauth_access_tokens", token)

    def delete_access_token(self, token: str) -> None:
        self._delete_token("oauth_access_tokens", token)

    # -- refresh tokens ---------------------------------------------------

    def save_refresh_token(
        self, token: str, client_id: str, scopes_json: str, expires_at: Optional[float]
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO oauth_refresh_tokens "
            "(token, client_id, scopes_json, expires_at) VALUES (?, ?, ?, ?)",
            (hash_token(token), client_id, scopes_json, expires_at),
        )
        self._conn.commit()

    def get_refresh_token(self, token: str) -> Optional[sqlite3.Row]:
        return self._get_token_row("oauth_refresh_tokens", token)

    def delete_refresh_token(self, token: str) -> None:
        self._delete_token("oauth_refresh_tokens", token)
