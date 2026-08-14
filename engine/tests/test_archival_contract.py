"""Archival-contract suggest mode: eligibility, lint, post-write flaws."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import yaml

from apo_engine import archival_contract, ops, vaults


def _write_archival(
    vault: Path,
    *,
    mode: str = "suggest",
    strategy: str = "mirror",
    older_than_days: int = 90,
    status_in: list[str] | None = None,
    include_folders: list[str] | None = None,
    deny_if_open_todos: bool = True,
) -> None:
    rel = archival_contract.ARCHIVAL_CONTRACT_REL
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "archival_contract_version": "0.1",
        "mode": mode,
        "destination": {"root": "archives", "strategy": strategy},
        "eligibility": {
            "include_folders": include_folders
            or ["areas/threads", "projects", "inbox"],
            "exempt_folders": ["system", "archives"],
            "exempt_globs": ["**/index.md"],
            "status_in": status_in or ["done", "archived", "resolved", "closed"],
            "idle": {"field": "last_activity", "older_than_days": older_than_days},
        },
        "actions": {
            "place": True,
            "set_fields": {"status": "archived", "archived_at": "$now"},
        },
        "safety": {"deny_if_open_todos": deny_if_open_todos},
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _note(
    vault: Path,
    rel: str,
    *,
    status: str = "done",
    last_activity: str = "2025-01-01T12:00:00Z",
    todos: list | None = None,
    body: str = "body\n",
) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fm: dict = {
        "title": path.stem,
        "status": status,
        "last_activity": last_activity,
    }
    if todos is not None:
        fm["todos"] = todos
    text = (
        "---\n"
        + yaml.safe_dump(fm, sort_keys=False).strip()
        + "\n---\n\n# "
        + path.stem
        + "\n\n"
        + body
    )
    path.write_text(text, encoding="utf-8")
    return path


def _mk_vault(root: Path, vault_id: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    cdir = root / "system" / "contracts"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "usage-contract.schema.yaml").write_text(
        f"vault_id: {vault_id}\n", encoding="utf-8"
    )
    return root


class ArchivalContractUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-archival-"))
        self.vault = self.tmp / "notes"
        self.vault.mkdir()
        self.now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mode_off_and_auto(self):
        _write_archival(self.vault, mode="off")
        data = archival_contract.load_archival_contract(self.vault)
        self.assertEqual(archival_contract.effective_mode(data), "off")
        self.assertEqual(archival_contract.raw_mode(data), "off")

        _write_archival(self.vault, mode="auto")
        data = archival_contract.load_archival_contract(self.vault)
        self.assertEqual(archival_contract.raw_mode(data), "auto")
        self.assertEqual(archival_contract.effective_mode(data), "off")

    def test_destination_mirror_only(self):
        _write_archival(self.vault, strategy="mirror")
        data = archival_contract.load_archival_contract(self.vault)
        self.assertEqual(
            archival_contract.destination_for("areas/threads/foo.md", data),
            "archives/areas/threads/foo.md",
        )
        _write_archival(self.vault, strategy="flat")
        data = archival_contract.load_archival_contract(self.vault)
        self.assertIsNone(
            archival_contract.destination_for("areas/threads/foo.md", data)
        )

    def test_eligible(self):
        _write_archival(self.vault)
        _note(self.vault, "areas/threads/cold.md", status="done")
        data = archival_contract.load_archival_contract(self.vault)
        flaw = archival_contract.evaluate_path(
            self.vault, "areas/threads/cold.md", data, scope="lint", now=self.now
        )
        self.assertIsNotNone(flaw)
        assert flaw is not None
        self.assertEqual(flaw["code"], "archive.eligible")
        self.assertEqual(
            flaw["suggested_op"]["ops"][0]["dst"],
            "archives/areas/threads/cold.md",
        )
        self.assertIsNone(flaw["evidence"]["set_fields"]["archived_at"])

    def test_blocked_todos(self):
        _write_archival(self.vault)
        _note(
            self.vault,
            "areas/threads/todo.md",
            status="done",
            todos=[{"text": "finish", "status": "pending"}],
        )
        data = archival_contract.load_archival_contract(self.vault)
        flaw = archival_contract.evaluate_path(
            self.vault, "areas/threads/todo.md", data, scope="write", now=self.now
        )
        self.assertIsNotNone(flaw)
        assert flaw is not None
        self.assertEqual(flaw["code"], "archive.blocked_todos")

    def test_blocked_status_lint_only(self):
        _write_archival(self.vault)
        _note(self.vault, "areas/threads/active.md", status="active")
        data = archival_contract.load_archival_contract(self.vault)
        lint_flaw = archival_contract.evaluate_path(
            self.vault, "areas/threads/active.md", data, scope="lint", now=self.now
        )
        self.assertIsNotNone(lint_flaw)
        assert lint_flaw is not None
        self.assertEqual(lint_flaw["code"], "archive.blocked_status")
        write_flaw = archival_contract.evaluate_path(
            self.vault, "areas/threads/active.md", data, scope="write", now=self.now
        )
        self.assertIsNone(write_flaw)

    def test_not_idle(self):
        _write_archival(self.vault, older_than_days=90)
        recent = (self.now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _note(self.vault, "areas/threads/hot.md", status="done", last_activity=recent)
        data = archival_contract.load_archival_contract(self.vault)
        self.assertIsNone(
            archival_contract.evaluate_path(
                self.vault, "areas/threads/hot.md", data, scope="lint", now=self.now
            )
        )


class ArchivalOpsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-archival-ops-"))
        self.vault = _mk_vault(self.tmp / "notes", "alpha")
        _write_archival(self.vault)
        self.now = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)

        self._env = mock.patch.dict(
            os.environ,
            {
                "APO_VAULT_PATHS": str(self.vault),
                "APO_DEFAULT_VAULT": "alpha",
            },
            clear=False,
        )
        self._env.start()
        vaults._vault_id_cache.clear()
        _default, bindings = vaults.load_bindings()
        self.assertEqual(set(bindings), {"alpha"}, bindings)
        self.assertEqual(_default, "alpha")
        ops._recent_touches.clear()

    def tearDown(self):
        self._env.stop()
        vaults._vault_id_cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lint_returns_eligible(self):
        _note(self.vault, "areas/threads/cold.md", status="done")
        out = archival_contract.lint_vault(
            self.vault,
            archival_contract.load_archival_contract(self.vault),
            limit=50,
            offset=0,
            vault_name="alpha",
            now=self.now,
        )
        self.assertTrue(out["ok"])
        codes = [f["code"] for f in out["flaws"]]
        self.assertIn("archive.eligible", codes)

    def test_lint_auto_tip(self):
        _write_archival(self.vault, mode="auto")
        out = ops.vault_op("lint", vault="alpha")
        self.assertTrue(out["ok"])
        self.assertEqual(out.get("tip"), archival_contract.AUTO_TIP)
        self.assertEqual(out["flaws"], [])

    def test_lint_pagination(self):
        for i in range(3):
            _note(
                self.vault,
                f"areas/threads/c{i}.md",
                status="done",
                last_activity="2025-01-01T00:00:00Z",
            )
        data = archival_contract.load_archival_contract(self.vault)
        page0 = archival_contract.lint_vault(
            self.vault, data, limit=2, offset=0, now=self.now, vault_name="alpha"
        )
        self.assertEqual(len(page0["flaws"]), 2)
        self.assertTrue(page0["has_more"])
        page1 = archival_contract.lint_vault(
            self.vault, data, limit=2, offset=2, now=self.now, vault_name="alpha"
        )
        self.assertEqual(len(page1["flaws"]), 1)
        self.assertFalse(page1["has_more"])

    def test_vault_op_lint(self):
        _note(self.vault, "areas/threads/cold.md", status="done")
        out = ops.vault_op("lint", vault="alpha", folder="areas/threads")
        self.assertTrue(out["ok"])
        self.assertTrue(any(f["code"] == "archive.eligible" for f in out["flaws"]))

    def test_write_attaches_eligible_flaw(self):
        rel = "areas/threads/cold.md"
        _note(self.vault, rel, status="done")
        out = ops.write_note(
            rel,
            content=(self.vault / rel).read_text(encoding="utf-8"),
            vault="alpha",
        )
        self.assertTrue(out["ok"], out)
        flaws = out.get("flaws") or []
        self.assertTrue(
            any(f.get("code") == "archive.eligible" for f in flaws), flaws
        )

    def test_write_active_idle_no_blocked_status(self):
        rel = "areas/threads/active.md"
        _note(self.vault, rel, status="active")
        out = ops.write_note(
            rel,
            content=(self.vault / rel).read_text(encoding="utf-8"),
            vault="alpha",
        )
        self.assertTrue(out["ok"], out)
        flaws = out.get("flaws") or []
        self.assertFalse(
            any(f.get("code") == "archive.blocked_status" for f in flaws), flaws
        )

    def test_bad_action_mentions_lint(self):
        out = ops.vault_op("explode")
        self.assertFalse(out["ok"])
        self.assertIn("lint", out["message"])


if __name__ == "__main__":
    unittest.main()
