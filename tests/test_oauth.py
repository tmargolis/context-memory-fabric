"""OAuth 2.1 authorization server coverage (MS6c auth layer).

Two layers:
- Provider-level tests against a temp OAuthStore: every state transition
  (consent -> code -> token -> refresh -> rotate -> revoke) and the refusal
  cases (wrong password, expired/reused/foreign code, expired/foreign/rotated
  refresh token, expired access token).
- One end-to-end HTTP flow through the real `server.mcp` app (register,
  authorize with PKCE, the consent page, token exchange, /mcp with and
  without a bearer token, refresh rotation). It runs in a subprocess because
  OAuth is configured from env vars at `server.mcp` import time; the
  subprocess swaps the provider's store for a temp DB, so the real
  journal.db is never touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from urllib.parse import parse_qs, urlparse

from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
from mcp.shared.auth import OAuthClientInformationFull
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from server.core import oauth_provider as oauth_mod
from server.core.http_auth import BearerTokenAuthMiddleware
from server.core.oauth_provider import CMFOAuthProvider
from server.core.oauth_store import OAuthStore, hash_token

PASSWORD = "correct-horse-battery-staple"
REDIRECT = "http://localhost/cb"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _client(client_id: str = "client-a") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_name=client_id,
        redirect_uris=[REDIRECT],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )


def _params(state: str = "st", resource: str | None = "http://localhost/mcp") -> AuthorizationParams:
    return AuthorizationParams(
        state=state,
        scopes=["mcp"],
        code_challenge="x" * 43,
        redirect_uri=REDIRECT,
        redirect_uri_provided_explicitly=True,
        resource=resource,
    )


class OAuthProviderBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OAuthStore(Path(self._tmp.name) / "journal.db")
        self.provider = CMFOAuthProvider(
            consent_base_url="http://localhost/", consent_password=PASSWORD, store=self.store
        )

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    async def _code_for(self, client: OAuthClientInformationFull) -> str:
        await self.provider.register_client(client)
        consent_url = await self.provider.authorize(client, _params())
        request_id = parse_qs(urlparse(consent_url).query)["request_id"][0]
        redirect = self.provider.approve(request_id)
        return parse_qs(urlparse(redirect).query)["code"][0]

    async def _tokens_for(self, client: OAuthClientInformationFull):
        code = await self._code_for(client)
        loaded = await self.provider.load_authorization_code(client, code)
        return await self.provider.exchange_authorization_code(client, loaded)


class TestRoundTrip(OAuthProviderBase):
    async def test_full_flow_consent_code_token_refresh_rotate(self):
        client = _client()
        await self.provider.register_client(client)
        self.assertEqual((await self.provider.get_client("client-a")).client_id, "client-a")

        consent_url = await self.provider.authorize(client, _params(state="abc"))
        self.assertTrue(consent_url.startswith("http://localhost/oauth/consent?request_id="))
        request_id = parse_qs(urlparse(consent_url).query)["request_id"][0]
        self.assertIsNotNone(self.provider.get_pending_request(request_id))

        self.assertTrue(self.provider.check_consent_password(PASSWORD))
        redirect = self.provider.approve(request_id)
        query = parse_qs(urlparse(redirect).query)
        self.assertEqual(query["state"], ["abc"])
        self.assertIsNone(self.provider.get_pending_request(request_id), "a pending request is single-use")

        loaded = await self.provider.load_authorization_code(client, query["code"][0])
        self.assertEqual(loaded.resource, "http://localhost/mcp")
        tokens = await self.provider.exchange_authorization_code(client, loaded)
        self.assertEqual(tokens.token_type, "Bearer")
        self.assertEqual(tokens.scope, "mcp")

        access = await self.provider.load_access_token(tokens.access_token)
        self.assertEqual(access.client_id, "client-a")
        self.assertEqual(access.scopes, ["mcp"])

        refresh = await self.provider.load_refresh_token(client, tokens.refresh_token)
        rotated = await self.provider.exchange_refresh_token(client, refresh, [])
        self.assertNotEqual(rotated.refresh_token, tokens.refresh_token)
        self.assertEqual(rotated.scope, "mcp", "an empty scope request keeps the original grant")
        self.assertIsNone(
            await self.provider.load_refresh_token(client, tokens.refresh_token),
            "the old refresh token must stop working once rotated",
        )
        self.assertIsNotNone(await self.provider.load_access_token(rotated.access_token))
        self.assertIsNotNone(await self.provider.load_refresh_token(client, rotated.refresh_token))

    async def test_tokens_survive_a_provider_restart(self):
        client = _client()
        tokens = await self._tokens_for(client)
        restarted = CMFOAuthProvider(consent_base_url="http://localhost", consent_password=PASSWORD, store=self.store)
        self.assertIsNotNone(await restarted.load_access_token(tokens.access_token))
        self.assertIsNotNone(await restarted.load_refresh_token(client, tokens.refresh_token))

    async def test_revoke_access_and_refresh(self):
        client = _client()
        tokens = await self._tokens_for(client)
        access = await self.provider.load_access_token(tokens.access_token)
        refresh = await self.provider.load_refresh_token(client, tokens.refresh_token)
        await self.provider.revoke_token(access)
        await self.provider.revoke_token(refresh)
        self.assertIsNone(await self.provider.load_access_token(tokens.access_token))
        self.assertIsNone(await self.provider.load_refresh_token(client, tokens.refresh_token))

    async def test_deny_redirects_with_access_denied(self):
        client = _client()
        consent_url = await self.provider.authorize(client, _params(state="s9"))
        request_id = parse_qs(urlparse(consent_url).query)["request_id"][0]
        query = parse_qs(urlparse(self.provider.deny(request_id)).query)
        self.assertEqual(query["error"], ["access_denied"])
        self.assertEqual(query["state"], ["s9"])
        self.assertIsNone(self.provider.deny(request_id), "a denied request can't be reused")


class TestRefusals(OAuthProviderBase):
    def test_wrong_password_is_refused(self):
        self.assertFalse(self.provider.check_consent_password("wrong"))
        self.assertFalse(self.provider.check_consent_password(""))

    def test_non_ascii_password_is_denied_not_raised(self):
        self.assertFalse(self.provider.check_consent_password("pässwörd"))
        unicode_provider = CMFOAuthProvider(
            consent_base_url="http://localhost", consent_password="pässwörd", store=self.store
        )
        self.assertTrue(unicode_provider.check_consent_password("pässwörd"))
        self.assertFalse(unicode_provider.check_consent_password("password"))

    async def test_expired_unexchanged_codes_are_pruned_when_a_new_code_is_issued(self):
        client = _client()
        stale = await self._code_for(client)
        self.provider._codes[stale].expires_at = time.time() - 1
        fresh = await self._code_for(client)
        self.assertNotIn(stale, self.provider._codes)
        self.assertIn(fresh, self.provider._codes)

    def test_approve_unknown_request_raises(self):
        with self.assertRaises(AuthorizeError):
            self.provider.approve("no-such-request")

    async def test_expired_pending_request_is_evicted(self):
        consent_url = await self.provider.authorize(_client(), _params())
        request_id = parse_qs(urlparse(consent_url).query)["request_id"][0]
        self.provider._pending[request_id].created_at -= oauth_mod._PENDING_REQUEST_TTL_SECONDS + 1
        self.assertIsNone(self.provider.get_pending_request(request_id))
        with self.assertRaises(AuthorizeError):
            self.provider.approve(request_id)

    async def test_expired_code_is_refused(self):
        client = _client()
        code = await self._code_for(client)
        self.provider._codes[code].expires_at = time.time() - 1
        self.assertIsNone(await self.provider.load_authorization_code(client, code))

    async def test_reused_code_is_refused(self):
        client = _client()
        code = await self._code_for(client)
        loaded = await self.provider.load_authorization_code(client, code)
        await self.provider.exchange_authorization_code(client, loaded)
        self.assertIsNone(await self.provider.load_authorization_code(client, code))

    async def test_code_issued_to_another_client_is_refused(self):
        code = await self._code_for(_client("client-a"))
        other = _client("client-b")
        await self.provider.register_client(other)
        self.assertIsNone(await self.provider.load_authorization_code(other, code))

    async def test_refresh_token_of_another_client_is_refused(self):
        tokens = await self._tokens_for(_client("client-a"))
        self.assertIsNone(await self.provider.load_refresh_token(_client("client-b"), tokens.refresh_token))

    async def test_expired_refresh_token_is_refused_and_deleted(self):
        client = _client()
        tokens = await self._tokens_for(client)
        self.store._conn.execute("UPDATE oauth_refresh_tokens SET expires_at = ?", (time.time() - 1,))
        self.store._conn.commit()
        self.assertIsNone(await self.provider.load_refresh_token(client, tokens.refresh_token))
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM oauth_refresh_tokens").fetchone()[0], 0)

    async def test_expired_access_token_is_refused_and_deleted(self):
        tokens = await self._tokens_for(_client())
        self.store._conn.execute("UPDATE oauth_access_tokens SET expires_at = ?", (time.time() - 1,))
        self.store._conn.commit()
        self.assertIsNone(await self.provider.load_access_token(tokens.access_token))
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM oauth_access_tokens").fetchone()[0], 0)

    async def test_unknown_tokens_are_refused(self):
        self.assertIsNone(await self.provider.load_access_token("nope"))
        self.assertIsNone(await self.provider.load_refresh_token(_client(), "nope"))


class TestTokensHashedAtRest(OAuthProviderBase):
    def _stored(self, table: str) -> list[str]:
        return [r[0] for r in self.store._conn.execute(f"SELECT token FROM {table}")]

    async def test_raw_tokens_never_hit_the_db(self):
        tokens = await self._tokens_for(_client())
        self.assertEqual(self._stored("oauth_access_tokens"), [hash_token(tokens.access_token)])
        self.assertEqual(self._stored("oauth_refresh_tokens"), [hash_token(tokens.refresh_token)])
        self.assertNotIn(tokens.access_token, self._stored("oauth_access_tokens"))
        access = await self.provider.load_access_token(tokens.access_token)
        self.assertEqual(access.token, tokens.access_token, "callers still see the raw token")

    def _insert_legacy(self, access: str, refresh: str) -> None:
        far = time.time() + 3600
        self.store._conn.execute(
            "INSERT INTO oauth_access_tokens (token, client_id, scopes_json, expires_at, resource) VALUES (?, 'client-a', '[]', ?, NULL)",
            (access, far),
        )
        self.store._conn.execute(
            "INSERT INTO oauth_refresh_tokens (token, client_id, scopes_json, expires_at) VALUES (?, 'client-a', '[]', ?)",
            (refresh, far),
        )
        self.store._conn.commit()

    async def test_legacy_plaintext_row_still_works_and_is_rehashed_on_lookup(self):
        self._insert_legacy("legacy-access-token-value", "legacy-refresh-token-value")
        self.assertIsNotNone(await self.provider.load_access_token("legacy-access-token-value"))
        self.assertIsNotNone(await self.provider.load_refresh_token(_client(), "legacy-refresh-token-value"))
        self.assertEqual(self._stored("oauth_access_tokens"), [hash_token("legacy-access-token-value")])
        self.assertEqual(self._stored("oauth_refresh_tokens"), [hash_token("legacy-refresh-token-value")])

    async def test_bulk_migration_is_idempotent_and_keeps_clients_connected(self):
        self._insert_legacy("legacy-access-token-value", "legacy-refresh-token-value")
        fresh = await self._tokens_for(_client())
        self.assertEqual(self.provider.hash_legacy_tokens(), 2, "only the 2 legacy rows are raw")
        self.assertEqual(self.provider.hash_legacy_tokens(), 0)
        self.assertTrue(all(len(t) == 64 for t in self._stored("oauth_access_tokens") + self._stored("oauth_refresh_tokens")))
        self.assertIsNotNone(await self.provider.load_access_token("legacy-access-token-value"))
        self.assertIsNotNone(await self.provider.load_access_token(fresh.access_token))

    async def test_revoking_a_legacy_token_deletes_the_raw_row(self):
        self._insert_legacy("legacy-access-token-value", "legacy-refresh-token-value")
        self.store.delete_access_token("legacy-access-token-value")
        self.assertEqual(self._stored("oauth_access_tokens"), [])


class TestBearerTokenMiddleware(unittest.TestCase):
    def setUp(self):
        app = Starlette(routes=[Route("/", lambda request: PlainTextResponse("ok"))])
        app.add_middleware(BearerTokenAuthMiddleware, token="s3cret")
        self.client = TestClient(app)

    def test_correct_token_passes(self):
        self.assertEqual(self.client.get("/", headers={"authorization": "Bearer s3cret"}).status_code, 200)

    def test_missing_wrong_or_malformed_token_is_401(self):
        for headers in ({}, {"authorization": "Bearer wrong"}, {"authorization": "Basic s3cret"}, {"authorization": "s3cret"}):
            self.assertEqual(self.client.get("/", headers=headers).status_code, 401, headers)

    def test_non_ascii_token_is_401_not_500(self):
        # Header values travel as latin-1; a non-ASCII token must deny cleanly.
        headers = {"authorization": "Bearer café".encode("latin-1")}
        self.assertEqual(self.client.get("/", headers=headers).status_code, 401)


_E2E_SCRIPT = textwrap.dedent(
    """
    import base64, hashlib, json, os, secrets, sys
    from pathlib import Path
    from urllib.parse import parse_qs, urlparse

    import server.mcp as m
    from server.core.oauth_store import OAuthStore
    from starlette.testclient import TestClient

    m.oauth_provider._store = OAuthStore(Path(sys.argv[1]))
    out = {}
    redirect = "http://localhost/cb"
    with TestClient(m.app.streamable_http_app(), base_url="http://localhost") as c:
        r = c.post("/register", json={
            "redirect_uris": [redirect], "client_name": "e2e", "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        })
        out["register"] = r.status_code
        cid = r.json()["client_id"]

        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        r = c.get("/authorize", follow_redirects=False, params={
            "response_type": "code", "client_id": cid, "redirect_uri": redirect,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "s1",
        })
        out["authorize"] = r.status_code
        rid = parse_qs(urlparse(r.headers["location"]).query)["request_id"][0]

        out["consent_get"] = c.get("/oauth/consent", params={"request_id": rid}).status_code
        out["consent_bad_password"] = c.post(
            "/oauth/consent", data={"request_id": rid, "action": "approve", "password": "wrong"}
        ).status_code
        r = c.post("/oauth/consent", follow_redirects=False,
                   data={"request_id": rid, "action": "approve", "password": os.environ["CMF_MCP_OAUTH_PASSWORD"]})
        out["consent_good_password"] = r.status_code
        q = parse_qs(urlparse(r.headers["location"]).query)
        out["state"] = q["state"][0]
        code = q["code"][0]

        token_form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect, "client_id": cid}
        out["token_bad_verifier"] = c.post("/token", data={**token_form, "code_verifier": "x" * 50}).status_code
        r = c.post("/token", data={**token_form, "code_verifier": verifier})
        out["token"] = r.status_code
        tokens = r.json()
        out["token_reused_code"] = c.post("/token", data={**token_form, "code_verifier": verifier}).status_code

        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}}}
        h = {"accept": "application/json, text/event-stream"}
        out["mcp_no_token"] = c.post("/mcp", json=init, headers=h).status_code
        out["mcp_bad_token"] = c.post("/mcp", json=init, headers={**h, "authorization": "Bearer nope"}).status_code
        out["mcp_good_token"] = c.post(
            "/mcp", json=init, headers={**h, "authorization": "Bearer " + tokens["access_token"]}
        ).status_code

        refresh_form = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": cid}
        r = c.post("/token", data=refresh_form)
        out["refresh"] = r.status_code
        out["refresh_rotated"] = r.json()["refresh_token"] != tokens["refresh_token"]
        out["refresh_reused"] = c.post("/token", data=refresh_form).status_code
    print("E2E_RESULT " + json.dumps(out))
    """
)


class TestEndToEndHTTP(unittest.TestCase):
    """The real server.mcp app, wired exactly as in production."""

    def test_full_http_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **os.environ,
                "CMF_MCP_ISSUER_URL": "http://localhost",
                "CMF_MCP_OAUTH_PASSWORD": PASSWORD,
            }
            proc = subprocess.run(
                [sys.executable, "-c", _E2E_SCRIPT, str(Path(tmp) / "journal.db")],
                cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180,
            )
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("E2E_RESULT ")]
        self.assertTrue(lines, f"e2e script failed:\n{proc.stderr[-3000:]}")
        out = json.loads(lines[-1][len("E2E_RESULT "):])

        self.assertEqual(out["register"], 201)
        self.assertEqual(out["authorize"], 302)
        self.assertEqual(out["consent_get"], 200)
        self.assertEqual(out["consent_bad_password"], 401)
        self.assertEqual(out["consent_good_password"], 302)
        self.assertEqual(out["state"], "s1")
        self.assertEqual(out["token_bad_verifier"], 400, "PKCE must reject a wrong code_verifier")
        self.assertEqual(out["token"], 200)
        self.assertEqual(out["token_reused_code"], 400)
        self.assertEqual(out["mcp_no_token"], 401)
        self.assertEqual(out["mcp_bad_token"], 401)
        # Past auth, the SDK's transport host check may still answer (e.g. 421
        # for an unlisted Host) -- what matters here is that it isn't 401.
        self.assertNotEqual(out["mcp_good_token"], 401)
        self.assertEqual(out["refresh"], 200)
        self.assertTrue(out["refresh_rotated"])
        self.assertEqual(out["refresh_reused"], 400)


if __name__ == "__main__":
    unittest.main()
