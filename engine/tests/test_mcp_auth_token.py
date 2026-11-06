"""Static bearer-token auth (APO_MCP_AUTH=token) for a tailnet-exposed instance.

Unit-checks ``StaticBearerTokenVerifier`` / ``load_token_digests`` and then
confirms the wiring into ``server.py`` gates /mcp: no token or a wrong token is
a 401, the right token gets through initialize.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from apo_engine.mcp_auth import (
    AuthConfigError,
    StaticBearerTokenVerifier,
    load_token_digests,
)

from test_mcp_auth_google import _HEADERS, _INIT_BODY, _WRITE_NOTE_BODY
from test_patch_note_schema import _list_tools_lean

OPTIMA_TOKEN = "optima-test-token-0123456789"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class StaticBearerTokenVerifierTest(unittest.TestCase):
    def setUp(self):
        self.verifier = StaticBearerTokenVerifier({"optima-service": _digest(OPTIMA_TOKEN)})

    def test_known_token_maps_to_client(self):
        access = asyncio.run(self.verifier.verify_token(OPTIMA_TOKEN))
        self.assertIsNotNone(access)
        self.assertEqual(access.client_id, "optima-service")

    def test_unknown_token_rejected(self):
        self.assertIsNone(asyncio.run(self.verifier.verify_token("nope")))

    def test_digest_itself_is_not_a_valid_token(self):
        self.assertIsNone(asyncio.run(self.verifier.verify_token(_digest(OPTIMA_TOKEN))))

    def test_empty_digest_map_raises(self):
        with self.assertRaises(AuthConfigError):
            StaticBearerTokenVerifier({})


class LoadTokenDigestsTest(unittest.TestCase):
    def _write(self, payload) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(payload, tmp)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return Path(tmp.name)

    def test_valid_file_parses(self):
        path = self._write({"clients": {"optima-service": {"sha256": _digest("x")}}})
        self.assertEqual(load_token_digests(path), {"optima-service": _digest("x")})

    def test_missing_file_raises(self):
        with self.assertRaises(AuthConfigError):
            load_token_digests(Path("/nonexistent/apo-tokens.json"))

    def test_bad_digest_raises(self):
        path = self._write({"clients": {"optima-service": {"sha256": "short"}}})
        with self.assertRaises(AuthConfigError):
            load_token_digests(path)


class TokenAuthGateTest(unittest.TestCase):
    """End-to-end: server.py wired with APO_MCP_AUTH=token gates /mcp."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tokens_path = Path(cls._tmp.name) / "mcp-tokens.json"
        tokens_path.write_text(
            json.dumps({"clients": {"optima-service": {"sha256": _digest(OPTIMA_TOKEN)}}})
        )
        os.environ["APO_MCP_AUTH"] = "token"
        os.environ["APO_MCP_TOKENS_FILE"] = str(tokens_path)
        try:
            cls.mod, _tools = _list_tools_lean(collection="mcp_token_gate_test")
        finally:
            os.environ.pop("APO_MCP_AUTH", None)
            os.environ.pop("APO_MCP_TOKENS_FILE", None)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _post(self, body, token: str | None = None):
        headers = dict(_HEADERS)
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        app = self.mod.mcp.http_app(host_origin_protection="auto")
        with TestClient(app, base_url="http://127.0.0.1") as client:
            return client.post("/mcp", json=body, headers=headers)

    def test_auth_provider_is_wired_onto_the_server(self):
        # _list_tools_lean reloads apo_engine modules, so compare by name, not identity.
        self.assertEqual(type(self.mod.mcp.auth).__name__, "StaticBearerTokenVerifier")

    def test_unauthenticated_write_note_rejected(self):
        self.assertEqual(self._post(_WRITE_NOTE_BODY).status_code, 401)

    def test_wrong_token_rejected(self):
        self.assertEqual(self._post(_INIT_BODY, token="wrong").status_code, 401)

    def test_valid_token_initializes(self):
        self.assertEqual(self._post(_INIT_BODY, token=OPTIMA_TOKEN).status_code, 200)


if __name__ == "__main__":
    unittest.main()
