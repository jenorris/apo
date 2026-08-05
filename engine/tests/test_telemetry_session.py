"""Telemetry contract + session_stats tests."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from apo_engine import telemetry_contract as tc
from apo_engine import tool_metrics


class TelemetryContractTest(unittest.TestCase):
    def test_policy_paths_vault_relative(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = root / "system" / "contracts"
            contract.mkdir(parents=True)
            (contract / "telemetry-contract.schema.yaml").write_text(
                yaml.safe_dump(
                    {
                        "telemetry_contract_version": "0.1",
                        "enabled": True,
                        "privacy": {
                            "allow": {
                                "paths": "vault_relative",
                                "headings": True,
                                "chunk_hash": True,
                            }
                        },
                        "agent_access": {"expose_paths": True},
                    }
                ),
                encoding="utf-8",
            )
            policy = tc.policy_for_vault(root)
            self.assertTrue(policy.allows_note_path())
            self.assertTrue(policy.expose_paths)
            ctx = tc.extract_note_context(
                "read_note",
                {"path": "areas/foo.md", "heading": "Intro"},
                policy,
            )
            self.assertEqual(ctx["note_path"], "areas/foo.md")
            self.assertIn("path_hash", ctx)
            self.assertEqual(ctx["heading"], "Intro")

    def test_disabled_contract_skips_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = root / "system" / "contracts"
            contract.mkdir(parents=True)
            (contract / "telemetry-contract.schema.yaml").write_text(
                yaml.safe_dump({"enabled": False}),
                encoding="utf-8",
            )
            db_path = Path(tmp) / "metrics.duckdb"
            tool_metrics.record_call(
                collection="c",
                tool="search_notes",
                ok=True,
                vault_root=root,
                path=db_path,
            )
            stats = tool_metrics.tool_stats("c", days=None, path=db_path)
            self.assertEqual(stats["calls"], 0)


class SessionStatsTest(unittest.TestCase):
    def test_session_stats_by_path_and_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = root / "system" / "contracts"
            contract.mkdir(parents=True)
            (contract / "telemetry-contract.schema.yaml").write_text(
                yaml.safe_dump(
                    {
                        "enabled": True,
                        "privacy": {"allow": {"paths": "vault_relative"}},
                        "agent_access": {"expose_paths": True},
                    }
                ),
                encoding="utf-8",
            )
            db_path = Path(tmp) / "metrics.duckdb"
            with mock.patch.dict(os.environ, {"APO_CONVERSATION_ID": "sess-1"}):
                tool_metrics.record_call(
                    collection="meta",
                    tool="read_note",
                    ok=True,
                    vault_root=root,
                    arguments={"path": "areas/hot.md"},
                    path=db_path,
                )
                tool_metrics.record_call(
                    collection="meta",
                    tool="patch_note",
                    ok=False,
                    error="validation_error",
                    vault_root=root,
                    arguments={"path": "areas/hot.md"},
                    path=db_path,
                )
            out = tool_metrics.session_stats(
                "meta", vault_root=root, conversation_id="sess-1", path=db_path
            )
            self.assertEqual(out["calls"], 2)
            self.assertEqual(out["conversation_id"], "sess-1")
            self.assertEqual(len(out["by_path"]), 1)
            self.assertEqual(out["by_path"][0]["path"], "areas/hot.md")
            self.assertEqual(out["by_path"][0]["calls"], 2)

    def test_active_session_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tc, "active_session_path", return_value=Path(tmp) / "nope.json"):
                out = tool_metrics.read_active_session()
                self.assertFalse(out["active"])

    def test_conversation_id_from_active_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "active-session.json"
            path.write_text(
                json.dumps({"conversation_id": "from-file"}),
                encoding="utf-8",
            )
            with mock.patch.object(tc, "active_session_path", return_value=path):
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(tc.conversation_id_from_env(), "from-file")


if __name__ == "__main__":
    unittest.main()
