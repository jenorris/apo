"""Server-side vault scoping for token clients (VaultScopeMiddleware).

The middleware is exercised through FastMCP's in-memory client against a
stand-in server whose tools are named like Apo's and echo the arguments they
receive, with the caller's access token patched in.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken

from apo_engine import vault_scope
from apo_engine.mcp_auth import AuthConfigError, StaticBearerTokenVerifier, load_token_scopes
from apo_engine.vault_scope import VaultScopeMiddleware

KNOWN = ["atlas", "grid", "lyra", "family"]


def _server() -> FastMCP:
    m = FastMCP("scope-test")

    @m.tool
    def search_notes(query: str, vault: str = "", vaults: list[str] | None = None,
                     folder: str = "", folders: list[str] | None = None) -> dict:
        return {"vault": vault, "vaults": vaults, "folder": folder, "folders": folders}

    @m.tool
    def read_note(path: str = "", chunk_hash: str = "", vault: str = "") -> dict:
        return {"vault": vault, "path": path}

    @m.tool
    def vault(request: dict) -> dict:
        return request

    @m.tool
    def apo_admin(action: str = "list") -> dict:
        return {"ok": True}

    @m.tool
    def some_future_tool() -> dict:
        return {"ok": True}

    m.add_middleware(VaultScopeMiddleware(lambda: KNOWN))
    return m


def _token(vaults=None, default=None):
    claims = {"apo_vaults": vaults, "apo_default_vault": default or vaults[0]} if vaults else {}
    return AccessToken(token="t", client_id="cecil", scopes=[], claims=claims)


def _call(token, tool, args):
    async def go():
        with mock.patch.object(vault_scope, "get_access_token", return_value=token):
            async with Client(_server()) as c:
                r = await c.call_tool(tool, args)
                return r.data if r.data is not None else json.loads(r.content[0].text)
    return asyncio.run(go())


def _tools(token):
    async def go():
        with mock.patch.object(vault_scope, "get_access_token", return_value=token):
            async with Client(_server()) as c:
                return sorted(t.name for t in await c.list_tools())
    return asyncio.run(go())


class ScopedClientTest(unittest.TestCase):
    tok = _token(["atlas", "grid"])

    def test_omitted_vault_becomes_client_default_not_server_default(self):
        self.assertEqual("atlas", _call(self.tok, "search_notes", {"query": "x"})["vault"])

    def test_in_scope_vault_passes(self):
        self.assertEqual("grid", _call(self.tok, "search_notes", {"query": "x", "vault": "grid"})["vault"])

    def test_out_of_scope_vault_is_refused(self):
        with self.assertRaisesRegex(ToolError, "vault_out_of_scope.*lyra"):
            _call(self.tok, "search_notes", {"query": "x", "vault": "lyra"})

    def test_out_of_scope_in_vaults_list_is_refused(self):
        with self.assertRaisesRegex(ToolError, "family"):
            _call(self.tok, "search_notes", {"query": "x", "vaults": ["atlas", "family"]})

    def test_prefixed_path_and_folders_are_checked(self):
        with self.assertRaisesRegex(ToolError, "lyra"):
            _call(self.tok, "read_note", {"path": "lyra:areas/secret.md"})
        with self.assertRaisesRegex(ToolError, "family"):
            _call(self.tok, "search_notes", {"query": "x", "folders": ["atlas:projects", "family:areas"]})
        # An in-scope prefix names the vault, so no default is injected on top.
        self.assertEqual("", _call(self.tok, "read_note", {"path": "grid:services/x.md"})["vault"])

    def test_colon_in_a_plain_path_is_not_a_vault(self):
        self.assertEqual("atlas", _call(self.tok, "read_note", {"path": "notes/10:30 standup.md"})["vault"])

    def test_vault_tool_multi_vault_actions_get_whole_scope(self):
        self.assertEqual(["atlas", "grid"], _call(self.tok, "vault", {"request": {"action": "project"}})["vaults"])

    def test_vault_tool_single_vault_actions_get_default(self):
        self.assertEqual("atlas", _call(self.tok, "vault", {"request": {"action": "stats"}})["vault"])

    def test_vault_tool_clone_target_is_checked(self):
        with self.assertRaisesRegex(ToolError, "lyra"):
            _call(self.tok, "vault", {"request": {"action": "clone", "vault": "atlas", "to": "lyra"}})

    def test_admin_and_unknown_tools_are_refused_and_hidden(self):
        with self.assertRaisesRegex(ToolError, "apo_admin"):
            _call(self.tok, "apo_admin", {})
        with self.assertRaisesRegex(ToolError, "some_future_tool"):
            _call(self.tok, "some_future_tool", {})
        self.assertEqual(["read_note", "search_notes", "vault"], _tools(self.tok))

    def test_explicit_default_vault(self):
        tok = _token(["atlas", "grid"], default="grid")
        self.assertEqual("grid", _call(tok, "search_notes", {"query": "x"})["vault"])


class UnscopedClientTest(unittest.TestCase):
    def test_no_token_is_untouched(self):
        self.assertEqual("", _call(None, "search_notes", {"query": "x"})["vault"])
        self.assertEqual("lyra", _call(None, "search_notes", {"query": "x", "vault": "lyra"})["vault"])
        self.assertIn("apo_admin", _tools(None))

    def test_token_without_vaults_is_untouched(self):
        self.assertEqual("family", _call(_token(), "search_notes", {"query": "x", "vault": "family"})["vault"])


class TokenScopeConfigTest(unittest.TestCase):
    def _file(self, clients) -> Path:
        d = tempfile.mkdtemp()
        p = Path(d) / "tokens.json"
        p.write_text(json.dumps({"clients": clients}))
        return p

    def test_scopes_load_and_reach_the_access_token(self):
        digest = hashlib.sha256(b"tok").hexdigest()
        p = self._file({
            "cecil": {"sha256": digest, "vaults": ["atlas", "grid"]},
            "optima": {"sha256": "0" * 64},
        })
        scopes = load_token_scopes(p)
        self.assertEqual({"cecil": {"vaults": ["atlas", "grid"], "default_vault": "atlas"}}, scopes)
        access = asyncio.run(StaticBearerTokenVerifier({"cecil": digest}, scopes).verify_token("tok"))
        self.assertEqual(["atlas", "grid"], access.claims["apo_vaults"])
        self.assertEqual("atlas", access.claims["apo_default_vault"])

    def test_unscoped_client_gets_no_claims(self):
        digest = hashlib.sha256(b"tok").hexdigest()
        access = asyncio.run(StaticBearerTokenVerifier({"optima": digest}).verify_token("tok"))
        self.assertEqual({}, access.claims)

    def test_bad_scope_entries_are_rejected(self):
        for entry, msg in [
            ({"vaults": []}, "non-empty"),
            ({"vaults": "atlas"}, "non-empty"),
            ({"vaults": ["atlas"], "default_vault": "lyra"}, "not in its vaults"),
        ]:
            with self.assertRaisesRegex(AuthConfigError, msg):
                load_token_scopes(self._file({"c": {"sha256": "0" * 64, **entry}}))


if __name__ == "__main__":
    unittest.main()
