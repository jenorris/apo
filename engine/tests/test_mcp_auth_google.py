"""Google multi-persona auth for the desma MCP endpoint — mocked OIDC, no network.

Exercises ``apo_engine.mcp_auth.PersonaGoogleTokenVerifier`` directly
(signature / issuer / audience / expiry / email-allowlist) against a
locally-minted RS256 ID token and a fake JWKS endpoint, then confirms the
wiring into ``server.py`` gates every tool call — including mutations like
``write_note`` — behind a valid bearer token when ``APO_MCP_AUTH=google``.
No network access or real Google OAuth client is used anywhere in this file.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

import httpx
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from joserfc import jwk
from starlette.testclient import TestClient

from apo_engine.mcp_auth import (
    AuthConfigError,
    Persona,
    PersonaGoogleTokenVerifier,
    build_auth_provider,
    load_persona_config,
)

from test_patch_note_schema import _list_tools_lean

JEREMY_CLIENT_ID = "jeremy-client.apps.googleusercontent.com"
FOTINI_CLIENT_ID = "fotini-client.apps.googleusercontent.com"
JEREMY_EMAIL = "jeremy.desma@gmail.com"
FOTINI_EMAIL = "fotini.desma@gmail.com"
KID = "test-key-1"


def _jwks_document(public_key_pem: str, kid: str) -> dict:
    key = jwk.import_key(public_key_pem, "RSA")
    data = key.as_dict()
    data["kid"] = kid
    data["use"] = "sig"
    data["alg"] = "RS256"
    return {"keys": [data]}


def _mock_http_client(jwks: dict) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=jwks)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _personas() -> tuple[Persona, ...]:
    return (
        Persona(
            name="jeremy",
            client_id=JEREMY_CLIENT_ID,
            client_secret=None,
            allowed_emails=frozenset({JEREMY_EMAIL}),
        ),
        Persona(
            name="fotini",
            client_id=FOTINI_CLIENT_ID,
            client_secret=None,
            allowed_emails=frozenset({FOTINI_EMAIL}),
        ),
    )


class PersonaVerifierTest(unittest.TestCase):
    """Every branch is a synthetic RS256 ID token verified against a mocked JWKS."""

    def setUp(self):
        self.keypair = RSAKeyPair.generate()
        self.other_keypair = RSAKeyPair.generate()  # signer NOT in the JWKS
        self.jwks = _jwks_document(self.keypair.public_key, KID)

    def _verifier(self) -> PersonaGoogleTokenVerifier:
        return PersonaGoogleTokenVerifier(
            personas=_personas(),
            issuer=["https://accounts.google.com", "accounts.google.com"],
            jwks_uri="https://mock.invalid/certs",
            http_client=_mock_http_client(self.jwks),
        )

    def _token(self, keypair, *, issuer, audience, email, expires_in=3600, kid=KID):
        return keypair.create_token(
            subject="sub-123",
            issuer=issuer,
            audience=audience,
            expires_in_seconds=expires_in,
            kid=kid,
            additional_claims={"email": email, "email_verified": True},
        )

    def test_valid_jeremy_token_maps_to_jeremy(self):
        token = self._token(
            self.keypair,
            issuer="https://accounts.google.com",
            audience=JEREMY_CLIENT_ID,
            email=JEREMY_EMAIL,
        )
        access = asyncio.run(self._verifier().verify_token(token))
        self.assertIsNotNone(access)
        self.assertEqual(access.claims["persona"], "jeremy")

    def test_valid_fotini_token_maps_to_fotini(self):
        token = self._token(
            self.keypair,
            issuer="https://accounts.google.com",
            audience=FOTINI_CLIENT_ID,
            email=FOTINI_EMAIL,
        )
        access = asyncio.run(self._verifier().verify_token(token))
        self.assertIsNotNone(access)
        self.assertEqual(access.claims["persona"], "fotini")

    def test_wrong_persona_client_id_rejected(self):
        # Jeremy's email, but the token was issued for Fotini's client
        # registration — must NOT fall back and authorize as Fotini either.
        token = self._token(
            self.keypair,
            issuer="https://accounts.google.com",
            audience=FOTINI_CLIENT_ID,
            email=JEREMY_EMAIL,
        )
        access = asyncio.run(self._verifier().verify_token(token))
        self.assertIsNone(access)

    def test_wrong_issuer_rejected(self):
        token = self._token(
            self.keypair,
            issuer="https://evil.example.com",
            audience=JEREMY_CLIENT_ID,
            email=JEREMY_EMAIL,
        )
        access = asyncio.run(self._verifier().verify_token(token))
        self.assertIsNone(access)

    def test_expired_token_rejected(self):
        token = self._token(
            self.keypair,
            issuer="https://accounts.google.com",
            audience=JEREMY_CLIENT_ID,
            email=JEREMY_EMAIL,
            expires_in=-10,
        )
        access = asyncio.run(self._verifier().verify_token(token))
        self.assertIsNone(access)

    def test_email_not_allowlisted_rejected(self):
        token = self._token(
            self.keypair,
            issuer="https://accounts.google.com",
            audience=JEREMY_CLIENT_ID,
            email="someone-else@gmail.com",
        )
        access = asyncio.run(self._verifier().verify_token(token))
        self.assertIsNone(access)

    def test_bad_signature_rejected(self):
        # Signed by a key that never appears in the JWKS response.
        token = self._token(
            self.other_keypair,
            issuer="https://accounts.google.com",
            audience=JEREMY_CLIENT_ID,
            email=JEREMY_EMAIL,
        )
        access = asyncio.run(self._verifier().verify_token(token))
        self.assertIsNone(access)


class PersonaConfigTest(unittest.TestCase):
    def test_missing_file_raises(self):
        with self.assertRaises(AuthConfigError):
            load_persona_config(Path("/nonexistent/mcp-auth.json"))

    def test_valid_config_parses_and_lowercases_emails(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "mcp-auth.json"
            p.write_text(
                json.dumps(
                    {
                        "personas": [
                            {
                                "name": "jeremy",
                                "client_id": JEREMY_CLIENT_ID,
                                "client_secret": "unused-in-verification",
                                "allowed_emails": [JEREMY_EMAIL.upper()],
                            }
                        ],
                        "issuer": "https://accounts.google.com",
                        "base_url": "https://desma-mcp.example.ts.net",
                    }
                )
            )
            settings = load_persona_config(p)
            self.assertEqual(settings.personas[0].name, "jeremy")
            self.assertIn(JEREMY_EMAIL, settings.personas[0].allowed_emails)

    def test_build_auth_provider_from_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "mcp-auth.json"
            p.write_text(
                json.dumps(
                    {
                        "personas": [
                            {
                                "name": "jeremy",
                                "client_id": JEREMY_CLIENT_ID,
                                "client_secret": "unused-in-verification",
                                "allowed_emails": [JEREMY_EMAIL],
                            },
                            {
                                "name": "fotini",
                                "client_id": FOTINI_CLIENT_ID,
                                "client_secret": "unused-in-verification",
                                "allowed_emails": [FOTINI_EMAIL],
                            },
                        ],
                        "issuer": "https://accounts.google.com",
                        "base_url": "https://desma-mcp.example.ts.net",
                    }
                )
            )
            provider = build_auth_provider(p)
            self.assertIsInstance(provider, RemoteAuthProvider)


_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
_WRITE_NOTE_BODY = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {"name": "write_note", "arguments": {"path": "x.md", "content": "hi"}},
}
_HEADERS = {"Accept": "application/json, text/event-stream"}


class UnauthenticatedMutationGateTest(unittest.TestCase):
    """End-to-end: server.py wired with APO_MCP_AUTH=google gates /mcp entirely."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="apo-mcp-auth-gate-")
        config_path = Path(cls._tmp.name) / "mcp-auth.json"
        config_path.write_text(
            json.dumps(
                {
                    "personas": [
                        {
                            "name": "jeremy",
                            "client_id": JEREMY_CLIENT_ID,
                            "client_secret": "unused-in-verification",
                            "allowed_emails": [JEREMY_EMAIL],
                        },
                        {
                            "name": "fotini",
                            "client_id": FOTINI_CLIENT_ID,
                            "client_secret": "unused-in-verification",
                            "allowed_emails": [FOTINI_EMAIL],
                        },
                    ],
                    "issuer": "https://accounts.google.com",
                    "base_url": "https://desma-mcp.example.ts.net",
                }
            )
        )
        os.environ["APO_MCP_AUTH"] = "google"
        os.environ["APO_MCP_AUTH_CONFIG"] = str(config_path)
        try:
            cls.mod, _tools = _list_tools_lean(collection="mcp_auth_gate_test")
        finally:
            os.environ.pop("APO_MCP_AUTH", None)
            os.environ.pop("APO_MCP_AUTH_CONFIG", None)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_auth_provider_is_wired_onto_the_server(self):
        self.assertIsNotNone(self.mod.mcp.auth)
        self.assertIsInstance(self.mod.mcp.auth, RemoteAuthProvider)

    def test_streamable_http_app_constructs_with_auth(self):
        # Confirms transport="http" (the entry point's mcp.run(transport="http", ...)
        # branch) is compatible with auth= being set — same http_app() the
        # __main__ block builds internally, without binding a real port.
        app = self.mod.mcp.http_app(host_origin_protection="auto")
        self.assertIsNotNone(app)

    def test_unauthenticated_initialize_rejected(self):
        app = self.mod.mcp.http_app(host_origin_protection="auto")
        with TestClient(app, base_url="http://127.0.0.1") as client:
            resp = client.post("/mcp", json=_INIT_BODY, headers=_HEADERS)
        self.assertEqual(resp.status_code, 401)

    def test_unauthenticated_write_note_mutation_rejected(self):
        app = self.mod.mcp.http_app(host_origin_protection="auto")
        with TestClient(app, base_url="http://127.0.0.1") as client:
            resp = client.post("/mcp", json=_WRITE_NOTE_BODY, headers=_HEADERS)
        self.assertEqual(resp.status_code, 401)

    def test_garbage_bearer_token_rejected(self):
        app = self.mod.mcp.http_app(host_origin_protection="auto")
        with TestClient(app, base_url="http://127.0.0.1") as client:
            resp = client.post(
                "/mcp",
                json=_WRITE_NOTE_BODY,
                headers={**_HEADERS, "Authorization": "Bearer not-a-real-jwt"},
            )
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
