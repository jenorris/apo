"""Token-client admin: the registry policy, hot reload, and /admin/clients over HTTP."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from apo_engine.mcp_auth import AuthConfigError, StaticBearerTokenVerifier
from apo_engine.token_admin import AdminError, AdminPolicy, TokenRegistry

from test_mcp_auth_google import _HEADERS, _INIT_BODY
from test_patch_note_schema import _list_tools_lean

ADMIN_TOKEN = "admin-test-token-0123456789"
OPTIMA_TOKEN = "optima-test-token-0123456789"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _tokens_file(dirpath: str) -> Path:
    path = Path(dirpath) / "mcp-tokens.json"
    path.write_text(json.dumps({"clients": {
        "optima-service": {"sha256": _digest(OPTIMA_TOKEN)},
        "ops-admin": {"sha256": _digest(ADMIN_TOKEN), "admin": {"prefix": "ops-", "vaults": ["atlas", "grid", "family"]}},
        "ops-locked": {"sha256": _digest("x"), "admin": {"prefix": "ops-", "vaults": ["atlas"]}},
    }}))
    return path


POLICY = AdminPolicy(prefix="ops-", vaults=frozenset({"atlas", "grid", "family"}))


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = _tokens_file(self.tmp.name)
        self.reg = TokenRegistry(self.path, lambda: ["atlas", "grid", "family", "lyra"])

    def clients(self) -> dict:
        return json.loads(self.path.read_text())["clients"]

    def test_issue_stores_only_a_digest_and_returns_the_token_once(self):
        out = self.reg.issue(POLICY, "ops-admin", "ops-lyra", ["atlas", "grid"], "atlas")
        self.assertEqual(len(out["token"]), 64)
        entry = self.clients()["ops-lyra"]
        self.assertEqual(entry["sha256"], _digest(out["token"]))
        self.assertNotIn(out["token"], self.path.read_text())
        self.assertEqual((entry["vaults"], entry["default_vault"], entry["issued_by"]), (["atlas", "grid"], "atlas", "ops-admin"))
        self.assertNotIn("token", self.reg.list(POLICY)[0])
        self.assertNotIn("sha256", self.reg.list(POLICY)[0])
        self.assertIn("optima-service", self.clients(), "other entries are kept")
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")

    def test_policy_limits(self):
        cases = [
            ("optima-x", ["atlas"], 403),          # outside the prefix
            ("ops-", ["atlas"], 403),            # the bare prefix
            ("Cecil-Bad", ["atlas"], 400),         # name shape
            ("ops-lyra", ["lyra"], 403),         # beyond the vault ceiling
            ("ops-lyra", ["nope"], 403),         # not in ceiling (checked first)
            ("ops-lyra", [], 400),
        ]
        for name, vaults, status in cases:
            with self.assertRaises(AdminError, msg=(name, vaults)) as cm:
                self.reg.issue(POLICY, "ops-admin", name, vaults)
            self.assertEqual(cm.exception.status, status, (name, vaults))
        with self.assertRaises(AdminError) as cm:
            self.reg.issue(POLICY, "ops-admin", "ops-lyra", ["atlas"], "grid")
        self.assertEqual(cm.exception.status, 400)

    def test_unknown_vaults_are_refused(self):
        reg = TokenRegistry(self.path, lambda: ["atlas"])
        with self.assertRaises(AdminError):
            reg.issue(POLICY, "ops-admin", "ops-lyra", ["family"])

    def test_admin_entries_are_untouchable(self):
        for op in (lambda: self.reg.rotate(POLICY, "a", "ops-locked"),
                   lambda: self.reg.revoke(POLICY, "a", "ops-locked"),
                   lambda: self.reg.rescope(POLICY, "a", "ops-locked", ["atlas"])):
            with self.assertRaises(AdminError) as cm:
                op()
            self.assertEqual(cm.exception.status, 403)
        self.assertNotIn("ops-locked", [c["name"] for c in self.reg.list(POLICY)])

    def test_issue_conflicts_rotate_rescope_revoke(self):
        first = self.reg.issue(POLICY, "a", "ops-lyra", ["atlas"])
        with self.assertRaises(AdminError) as cm:
            self.reg.issue(POLICY, "a", "ops-lyra", ["atlas"])
        self.assertEqual(cm.exception.status, 409)
        rotated = self.reg.rotate(POLICY, "a", "ops-lyra")
        self.assertNotEqual(rotated["token"], first["token"])
        self.assertEqual(self.clients()["ops-lyra"]["sha256"], _digest(rotated["token"]))
        self.assertEqual(self.reg.rescope(POLICY, "a", "ops-lyra", ["grid", "atlas"])["default_vault"], "grid")
        self.reg.revoke(POLICY, "a", "ops-lyra")
        self.assertNotIn("ops-lyra", self.clients())
        with self.assertRaises(AdminError) as cm:
            self.reg.revoke(POLICY, "a", "ops-lyra")
        self.assertEqual(cm.exception.status, 404)

    def test_clients_outside_the_ceiling_are_not_managed(self):
        """A prefix match is not enough: rotating an unscoped or wider client would escalate."""
        data = json.loads(self.path.read_text())
        data["clients"]["ops-unscoped"] = {"sha256": _digest("u")}  # every vault
        data["clients"]["ops-wide"] = {"sha256": _digest("w"), "vaults": ["atlas", "lyra"], "default_vault": "atlas"}
        self.path.write_text(json.dumps(data))
        for name in ("ops-unscoped", "ops-wide"):
            for op in (lambda: self.reg.rotate(POLICY, "a", name),
                       lambda: self.reg.revoke(POLICY, "a", name),
                       lambda: self.reg.rescope(POLICY, "a", name, ["atlas"])):
                with self.assertRaises(AdminError) as cm:
                    op()
                self.assertEqual(cm.exception.status, 403, name)
            self.assertNotIn(name, [c["name"] for c in self.reg.list(POLICY)])
        clients = self.clients()
        self.assertEqual(clients["ops-unscoped"]["sha256"], _digest("u"), "not rotated")
        self.assertNotIn("vaults", clients["ops-unscoped"])
        self.assertEqual(clients["ops-wide"]["vaults"], ["atlas", "lyra"])

    def test_bad_admin_policy_is_a_config_error(self):
        self.path.write_text(json.dumps({"clients": {"x": {"sha256": _digest("x"), "admin": {"prefix": "noDash", "vaults": ["atlas"]}}}}))
        with self.assertRaises(AuthConfigError):
            StaticBearerTokenVerifier.from_file(self.path)


class VerifierReloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = _tokens_file(self.tmp.name)
        self.verifier = StaticBearerTokenVerifier.from_file(self.path)

    def verify(self, token):
        return asyncio.run(self.verifier.verify_token(token))

    def test_admin_tokens_are_refused_on_mcp_but_accepted_as_admin(self):
        self.assertIsNone(self.verify(ADMIN_TOKEN))
        name, policy = self.verifier.verify_admin(ADMIN_TOKEN)
        self.assertEqual((name, policy.prefix), ("ops-admin", "ops-"))
        self.assertIsNone(self.verifier.verify_admin(OPTIMA_TOKEN), "an ordinary client is not an admin")

    def test_issued_and_revoked_clients_take_effect_without_restart(self):
        reg = TokenRegistry(self.path)
        out = reg.issue(POLICY, "ops-admin", "ops-lyra", ["atlas", "grid"])
        access = self.verify(out["token"])
        self.assertEqual(access.client_id, "ops-lyra")
        self.assertEqual(access.claims["apo_vaults"], ["atlas", "grid"])
        reg.revoke(POLICY, "ops-admin", "ops-lyra")
        self.assertIsNone(self.verify(out["token"]))

    def test_a_broken_edit_keeps_the_previous_clients(self):
        time.sleep(0.01)
        self.path.write_text("{not json")
        self.assertEqual(self.verify(OPTIMA_TOKEN).client_id, "optima-service")


class AdminHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.path = _tokens_file(cls._tmp.name)
        os.environ["APO_MCP_AUTH"] = "token"
        os.environ["APO_MCP_TOKENS_FILE"] = str(cls.path)
        try:
            cls.mod, _tools = _list_tools_lean(collection="mcp_token_admin_test")
        finally:
            os.environ.pop("APO_MCP_AUTH", None)
        # The routes resolve unknown vaults against the server's registry; allow the test names.
        cls.mod.VAULTS.update({v: object() for v in ("atlas", "grid", "family") if v not in cls.mod.VAULTS})

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("APO_MCP_TOKENS_FILE", None)
        cls._tmp.cleanup()

    def client(self):
        return TestClient(self.mod.mcp.http_app(host_origin_protection="auto"), base_url="http://127.0.0.1")

    def auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_admin_routes_need_an_admin_token(self):
        with self.client() as c:
            self.assertEqual(c.get("/admin/clients").status_code, 401)
            self.assertEqual(c.get("/admin/clients", headers=self.auth(OPTIMA_TOKEN)).status_code, 401)
            self.assertEqual(c.get("/admin/clients", headers=self.auth(ADMIN_TOKEN)).status_code, 200)

    def test_issue_then_use_on_mcp_then_revoke(self):
        with self.client() as c:
            r = c.post("/admin/clients", headers=self.auth(ADMIN_TOKEN), json={"name": "ops-http", "vaults": ["family"]})
            self.assertEqual(r.status_code, 201, r.text)
            self.assertEqual(r.headers.get("cache-control"), "no-store")
            token = r.json()["token"]
            self.assertEqual(r.json()["default_vault"], "family")

            init = c.post("/mcp", json=_INIT_BODY, headers=dict(_HEADERS) | self.auth(token))
            self.assertEqual(init.status_code, 200, "a freshly issued client works without a restart")
            self.assertEqual(c.post("/mcp", json=_INIT_BODY, headers=dict(_HEADERS) | self.auth(ADMIN_TOKEN)).status_code, 401,
                             "an admin token is not an MCP client")

            listed = c.get("/admin/clients", headers=self.auth(ADMIN_TOKEN)).json()["clients"]
            self.assertIn("ops-http", [x["name"] for x in listed])
            self.assertNotIn(token, json.dumps(listed))

            self.assertEqual(c.post("/admin/clients", headers=self.auth(ADMIN_TOKEN), json={"name": "ops-http2", "vaults": ["lyra"]}).status_code, 403)
            self.assertEqual(c.put("/admin/clients/ops-http", headers=self.auth(ADMIN_TOKEN), json={"vaults": ["family", "grid"]}).json()["vaults"], ["family", "grid"])
            self.assertEqual(c.delete("/admin/clients/ops-http", headers=self.auth(ADMIN_TOKEN)).status_code, 204)
            self.assertEqual(c.post("/mcp", json=_INIT_BODY, headers=dict(_HEADERS) | self.auth(token)).status_code, 401, "revoked")


if __name__ == "__main__":
    unittest.main()
