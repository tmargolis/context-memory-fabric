"""Minimal single-user OAuth 2.1 authorization server for CMF (MS6c).

Implements mcp.server.auth.provider.OAuthAuthorizationServerProvider so
Claude Desktop, ChatGPT, and Gemini can authenticate the way their own
connector UIs actually expect -- Dynamic Client Registration (RFC 7591) plus
an authorization-code + PKCE flow -- rather than the static bearer-token
header none of those UIs turned out to have a field for. server.core.http_auth's
BearerTokenAuthMiddleware remains for direct/manual HTTP access, but the two
are mutually exclusive at the transport level (see server/mcp.py): once this
provider is configured, it -- not the static token -- is what protects the
MCP endpoint, since the SDK's own auth middleware takes over that job.

Single-user design, deliberately not a general-purpose auth server: there is
no login system, no per-user accounts, no consent persisted across clients.
The one human who can approve a new client is whoever knows
CMF_MCP_OAUTH_PASSWORD, entered once per client on the consent page this
module drives (served at GET/POST /oauth/consent, registered in
server/mcp.py via @app.custom_route). That password is the actual security
boundary: both /register and /authorize are unauthenticated by spec, so
anyone who finds the server's URL can run the flow up through the consent
page themselves -- the pending request's own long random ID is not itself a
secret, it just avoids collisions between concurrent attempts.

Authorization codes and pending consent requests live only in memory, not in
server.core.oauth_store's sqlite file: both are short-lived by design (a
code must be exchanged within minutes; a pending request exists only
between /authorize and the consent page being submitted), so losing them on
a server restart is correct -- the client just retries -- not a gap worth
sqlite durability for. Access and refresh tokens, which clients expect to
keep working across server restarts, ARE persisted there.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from typing import Optional, Union

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from server.core.oauth_store import OAuthStore

_AUTH_CODE_TTL_SECONDS = 5 * 60
_PENDING_REQUEST_TTL_SECONDS = 10 * 60
ACCESS_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days -- personal, low-churn clients
REFRESH_TOKEN_TTL_SECONDS = 180 * 24 * 60 * 60  # 180 days


@dataclass
class PendingRequest:
    client: OAuthClientInformationFull
    params: AuthorizationParams
    created_at: float


class CMFOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, *, consent_base_url: str, consent_password: str, store: Optional[OAuthStore] = None) -> None:
        self._consent_base_url = consent_base_url.rstrip("/")
        self._consent_password = consent_password
        self._store = store or OAuthStore()
        self._pending: dict[str, PendingRequest] = {}
        self._codes: dict[str, AuthorizationCode] = {}

    # -- client registration (RFC 7591) --------------------------------

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        raw = self._store.get_client(client_id)
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._store.save_client(client_info.client_id, client_info.model_dump_json())

    # -- authorization: redirect to our own consent page -----------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self._evict_expired_pending()
        request_id = secrets.token_urlsafe(32)
        self._pending[request_id] = PendingRequest(client=client, params=params, created_at=time.time())
        return f"{self._consent_base_url}/oauth/consent?request_id={request_id}"

    def get_pending_request(self, request_id: str) -> Optional[PendingRequest]:
        self._evict_expired_pending()
        return self._pending.get(request_id)

    def check_consent_password(self, password: str) -> bool:
        return secrets.compare_digest(password, self._consent_password)

    def approve(self, request_id: str) -> str:
        """Mint the authorization code; called by the consent POST handler
        after password verification. Returns the redirect_uri to send the
        browser to next -- the SDK's /token handler takes over from there.
        """
        pending = self._pending.pop(request_id, None)
        if pending is None:
            raise AuthorizeError(error="access_denied", error_description="Consent request expired or unknown")

        code = secrets.token_urlsafe(32)  # 256 bits, well over RFC 6749's 128-bit floor
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=pending.params.scopes or [],
            expires_at=time.time() + _AUTH_CODE_TTL_SECONDS,
            client_id=pending.client.client_id,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=pending.params.resource,
        )
        return construct_redirect_uri(str(pending.params.redirect_uri), code=code, state=pending.params.state)

    def deny(self, request_id: str) -> Optional[str]:
        pending = self._pending.pop(request_id, None)
        if pending is None:
            return None
        return construct_redirect_uri(
            str(pending.params.redirect_uri), error="access_denied", state=pending.params.state
        )

    def _evict_expired_pending(self) -> None:
        cutoff = time.time() - _PENDING_REQUEST_TTL_SECONDS
        for rid in [rid for rid, p in self._pending.items() if p.created_at < cutoff]:
            del self._pending[rid]

    # -- authorization code exchange -------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[AuthorizationCode]:
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        if code.expires_at < time.time():
            self._codes.pop(authorization_code, None)
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)  # single-use

        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        scopes_json = json.dumps(authorization_code.scopes)

        self._store.save_access_token(
            access_token,
            client.client_id,
            scopes_json,
            time.time() + ACCESS_TOKEN_TTL_SECONDS,
            authorization_code.resource,
        )
        self._store.save_refresh_token(
            refresh_token, client.client_id, scopes_json, time.time() + REFRESH_TOKEN_TTL_SECONDS
        )

        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
            refresh_token=refresh_token,
        )

    # -- refresh -----------------------------------------------------------

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> Optional[RefreshToken]:
        row = self._store.get_refresh_token(refresh_token)
        if row is None or row["client_id"] != client.client_id:
            return None
        if row["expires_at"] is not None and row["expires_at"] < time.time():
            self._store.delete_refresh_token(refresh_token)
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes_json"]),
            expires_at=int(row["expires_at"]) if row["expires_at"] else None,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        self._store.delete_refresh_token(refresh_token.token)  # rotate, per RFC 6749 best practice

        granted_scopes = scopes or refresh_token.scopes
        new_access = secrets.token_urlsafe(32)
        new_refresh = secrets.token_urlsafe(32)
        scopes_json = json.dumps(granted_scopes)

        self._store.save_access_token(
            new_access, client.client_id, scopes_json, time.time() + ACCESS_TOKEN_TTL_SECONDS, None
        )
        self._store.save_refresh_token(new_refresh, client.client_id, scopes_json, time.time() + REFRESH_TOKEN_TTL_SECONDS)

        return OAuthToken(
            access_token=new_access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(granted_scopes) if granted_scopes else None,
            refresh_token=new_refresh,
        )

    # -- token verification (drives ProviderTokenVerifier too) -------------

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        row = self._store.get_access_token(token)
        if row is None:
            return None
        if row["expires_at"] is not None and row["expires_at"] < time.time():
            self._store.delete_access_token(token)
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes_json"]),
            expires_at=int(row["expires_at"]) if row["expires_at"] else None,
            resource=row["resource"],
        )

    async def revoke_token(self, token: Union[AccessToken, RefreshToken]) -> None:
        if isinstance(token, AccessToken):
            self._store.delete_access_token(token.token)
        else:
            self._store.delete_refresh_token(token.token)
