"""Contract mechanism tightening: shape checks, table-contract enforcement, lint parity.

Covers the gaps closed in the tighten-contracts pass:

* ``table-contract`` — mapping-form ``tables:`` (the shape the Optima vault
  actually ships) is honored; ``replace_table`` upsert keys by the contract
  ``key_column``; per-rule ``merge`` / ``allow_new_columns`` / ``header_synonyms``
  are read (the template documented them, the engine ignored them).
* ``vault(contracts)`` / ``vault(lint)`` — advisory ``contract.*`` findings for
  keys the engine never reads, bad enums, and wrong container shapes.
* ``vault(lint)`` — runs the OKF producer profile per note, so MCP lint and
  ``apo-engine okf validate`` report the same corpus.
* shared contract YAML cache — parse once per mtime, copies not shared.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from apo_engine import (
    config,
    core,
    note_lint,
    okf,
    okf_cli,
    ops,
    search_contract,
    table_contract,
    vault_contracts,
)

_DIM = 16


def _fake_embed(texts: list[str], **kwargs) -> list[list[float]]:
    out = []
    for t in texts:
        v = [0.0] * _DIM
        for tok in re.findall(r"\w+", t.lower()):
            slot = int(hashlib.md5(tok.encode()).hexdigest(), 16) % _DIM
            v[slot] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        out.append([x / norm for x in v])
    return out


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_yaml(path: Path, data: dict) -> None:
    _write(path, yaml.safe_dump(data, sort_keys=False))


# --------------------------------------------------------------------------- table


class TableContractShapeTest(unittest.TestCase):
    def setUp(self):
        vault_contracts.clear_yaml_cache()
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-tc-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.contract = self.vault / table_contract.TABLE_CONTRACT_REL

    def tearDown(self):
        vault_contracts.clear_yaml_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mapping_form_is_honored(self):
        # The live Optima vault ships this shape; the loader used to drop it
        # silently, so row_key fell back to the first cell.
        _write_yaml(
            self.contract,
            {
                "table_contract_version": "0.1",
                "vault_id": "optima",
                "tables": {"horizon.md": {"key_column": "start"}, "recent.md": {"key_column": "start"}},
            },
        )
        self.assertEqual(table_contract.key_column_for(self.vault, "horizon.md"), "start")
        self.assertEqual(table_contract.key_column_for(self.vault, "recent.md"), "start")
        self.assertIsNone(table_contract.key_column_for(self.vault, "other.md"))
        self.assertEqual(vault_contracts.check_contract("table-contract", yaml.safe_load(self.contract.read_text())), [])

    def test_list_form_first_match_wins(self):
        _write_yaml(
            self.contract,
            {
                "tables": [
                    {"match": "areas/vehicle/*.md", "key_column": "Date"},
                    {"match": "areas/**/*.md", "key_column": "SKU"},
                    {"key_column": "dropped-no-match"},
                ]
            },
        )
        self.assertEqual(table_contract.key_column_for(self.vault, "areas/vehicle/car.md"), "Date")
        self.assertEqual(table_contract.key_column_for(self.vault, "areas/home/x.md"), "SKU")
        self.assertEqual(len(table_contract.normalize_rules(yaml.safe_load(self.contract.read_text()))), 2)

    def test_check_contract_findings(self):
        data = {
            "table_contract_version": "0.1",
            "bogus_top": 1,
            "tables": [
                {"match": "a.md", "merge": "smash", "header_synonyms": ["not", "a", "map"], "nope": 1},
                {"key_column": "X"},
            ],
        }
        found = table_contract.check_contract(data)
        codes = {(f["code"], f["field"]) for f in found}
        self.assertIn(("contract.unknown_key", "bogus_top"), codes)
        self.assertIn(("contract.invalid_value", "tables[0].merge"), codes)
        self.assertIn(("contract.invalid_shape", "tables[0].header_synonyms"), codes)
        self.assertIn(("contract.unknown_key", "tables[0].nope"), codes)
        self.assertIn(("contract.invalid_shape", "tables[1]"), codes)
        bad_shape = table_contract.check_contract({"tables": "oops"})
        self.assertEqual([f["code"] for f in bad_shape], ["contract.invalid_shape"])

    def test_cache_invalidates_on_edit(self):
        _write_yaml(self.contract, {"tables": {"a.md": {"key_column": "One"}}})
        self.assertEqual(table_contract.key_column_for(self.vault, "a.md"), "One")
        _write_yaml(self.contract, {"tables": {"a.md": {"key_column": "Two"}}})
        st = self.contract.stat()
        os.utime(self.contract, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        self.assertEqual(table_contract.key_column_for(self.vault, "a.md"), "Two")


INVENTORY = """---
title: Inventory
---

## Stock

| Item   | SKU | Qty |
| ------ | --- | --- |
| Widget | A1  | 3   |
| Gadget | B2  | 5   |
"""


class ReplaceTableContractTest(unittest.TestCase):
    """``replace_table`` honors the table-contract rule that matches the note."""

    def setUp(self):
        vault_contracts.clear_yaml_cache()
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-rt-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        _write(self.vault / "inventory.md", INVENTORY)
        self.index = self.tmp / "index.db"
        self._patches = [
            mock.patch.object(config, "NOTES_ROOT", self.vault),
            mock.patch.object(config, "INDEX_PATH", self.index),
            mock.patch.object(config, "COLLECTION", "replace_table_contract"),
            mock.patch.object(config, "VAULTS_CONFIG", ""),
            mock.patch.object(core, "embed", _fake_embed),
            mock.patch.object(core, "query_embed", lambda q: _fake_embed([q])[0]),
        ]
        for p in self._patches:
            p.start()
        core.index_vault(rebuild=True, verbose=False)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        core.writer_close()
        core.reader_close()
        vault_contracts.clear_yaml_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _contract(self, rule: dict) -> None:
        _write_yaml(
            self.vault / table_contract.TABLE_CONTRACT_REL,
            {"table_contract_version": "0.1", "tables": [{"match": "inventory.md", **rule}]},
        )

    def _rows(self) -> list[list[str]]:
        text = (self.vault / "inventory.md").read_text(encoding="utf-8")
        rows = [
            [c.strip() for c in line.strip().strip("|").split("|")]
            for line in text.splitlines()
            if line.startswith("|")
        ]
        return rows[2:]  # header + delimiter

    def test_upsert_keys_by_contract_key_column(self):
        self._contract({"key_column": "SKU", "merge": "upsert"})
        out = ops.patch_note(
            "inventory.md",
            [{"op": "replace_table", "rows": [{"Item": "Widget v2", "SKU": "A1", "Qty": "4"}]}],
        )
        self.assertTrue(out["ok"], out)
        rows = self._rows()
        # Keyed on SKU (not the first cell): A1 was replaced, not appended.
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual(rows[0], ["Widget v2", "A1", "4"])
        self.assertEqual(rows[1], ["Gadget", "B2", "5"])

    def test_upsert_without_contract_keys_by_first_cell(self):
        # Baseline: no contract → natural key is the first non-empty cell, so a
        # changed Item is a new row. Documents why key_column matters.
        out = ops.patch_note(
            "inventory.md",
            [{"op": "replace_table", "merge": "upsert", "rows": [{"Item": "Widget v2", "SKU": "A1", "Qty": "4"}]}],
        )
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(self._rows()), 3)

    def test_op_merge_overrides_contract_default(self):
        self._contract({"key_column": "SKU", "merge": "upsert"})
        out = ops.patch_note(
            "inventory.md",
            [{"op": "replace_table", "merge": "append", "rows": [{"Item": "Widget", "SKU": "A1", "Qty": "9"}]}],
        )
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(self._rows()), 3)

    def test_header_synonyms_map_before_fuzzy(self):
        # "stock" cannot fuzzy-match "Qty"; the contract synonym resolves it.
        self._contract({"key_column": "SKU", "merge": "upsert", "header_synonyms": {"stock": "Qty"}})
        out = ops.patch_note(
            "inventory.md",
            [{"op": "replace_table", "rows": [{"item": "Gizmo", "sku": "C3", "stock": "7"}]}],
        )
        self.assertTrue(out["ok"], out)
        self.assertEqual(self._rows()[2], ["Gizmo", "C3", "7"])
        # Same import without the synonym is still ambiguity-rejected.
        self._contract({"key_column": "SKU", "merge": "upsert"})
        out = ops.patch_note(
            "inventory.md",
            [{"op": "replace_table", "rows": [{"item": "Gizmo", "sku": "D4", "stock": "1"}]}],
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "header_ambiguous")

    def test_allow_new_columns_default_from_contract(self):
        self._contract({"key_column": "SKU", "merge": "append", "allow_new_columns": True})
        out = ops.patch_note(
            "inventory.md",
            [{"op": "replace_table", "rows": [{"Item": "Gizmo", "SKU": "C3", "Qty": "7", "Color": "red"}]}],
        )
        self.assertTrue(out["ok"], out)
        text = (self.vault / "inventory.md").read_text(encoding="utf-8")
        self.assertIn("Color", text)
        self.assertIn("red", text)


# ------------------------------------------------------------------ shape checks


class ContractCheckTest(unittest.TestCase):
    def setUp(self):
        vault_contracts.clear_yaml_cache()
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-cc-"))
        self.vault = self.tmp / "vault"
        self.cdir = self.vault / "system" / "contracts"
        self.cdir.mkdir(parents=True)

    def tearDown(self):
        vault_contracts.clear_yaml_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_okf_contract_drift_is_reported(self):
        # Mirrors real drift: a live vault declares 0.2 provenance keys the
        # engine never reads, and an enforcement level the loader coerces to soft.
        _write(
            self.cdir / "okf-contract.schema.yaml",
            "okf_version: '0.2'\nbundle_root: '~/x'\n"
            "default_enforcement: strict\n"
            "provenance_optional: [sources, generated]\n"
            "path_rules:\n"
            "  - match: 'areas/**/*.md'\n    enforcement: Hard\n    class: reserved\n"
            "  - okf_type: NoMatch\n",
        )
        found = vault_contracts.discover_contracts(self.vault)
        entry = found["okf-contract"]
        self.assertTrue(entry["ok"])
        codes = {(w["code"], w["field"]) for w in entry["warnings"]}
        self.assertIn(("contract.unknown_key", "provenance_optional"), codes)
        self.assertIn(("contract.invalid_value", "default_enforcement"), codes)
        self.assertIn(("contract.unknown_key", "path_rules[0].class"), codes)
        self.assertIn(("contract.invalid_shape", "path_rules[1]"), codes)
        # Case-insensitive enum: "Hard" is fine (loader lowercases).
        self.assertNotIn(("contract.invalid_value", "path_rules[0].enforcement"), codes)
        # bundle_root is a known documentation key, not drift.
        self.assertNotIn(("contract.unknown_key", "bundle_root"), codes)
        # Summaries carry the warnings too (vault(contracts) default view).
        summary = vault_contracts.present_contracts(found, full=False)["okf-contract"]
        self.assertEqual(summary["warnings"], entry["warnings"])

    def test_clean_contracts_have_no_warnings_key(self):
        _write_yaml(
            self.cdir / "search-contract.schema.yaml",
            {"search_contract_version": "0.1", "default_exclude": ["archives/*"]},
        )
        _write_yaml(
            self.cdir / "git-contract.schema.yaml",
            {"git_contract_version": "0.1", "remote": "https://x.git", "host": "github", "sync": {"enabled": False}},
        )
        _write_yaml(
            self.cdir / "usage-contract.schema.yaml",
            {"usage_contract_version": "0.1", "vault_id": "v", "anything_goes": {"x": 1}},
        )
        found = vault_contracts.discover_contracts(self.vault)
        for cid in ("search-contract", "git-contract", "usage-contract"):
            self.assertNotIn("warnings", found[cid], cid)
            self.assertNotIn("warnings", vault_contracts.summarize_entry(found[cid]))

    def test_search_git_telemetry_mermaid_findings(self):
        _write_yaml(
            self.cdir / "search-contract.schema.yaml",
            {"default_exclude": "archives/*", "folder_exclude": [{"folder": "areas"}]},
        )
        _write_yaml(
            self.cdir / "git-contract.schema.yaml",
            {"host": "svn", "sync": {"enabled": True, "debounce_seconds": "soon", "push": True}},
        )
        _write_yaml(
            self.cdir / "telemetry-contract.schema.yaml",
            {"store": {"backend": "sqlite"}, "privacy": {"allow": {"paths": "everything"}}},
        )
        _write_yaml(
            self.cdir / "mermaid-contract.schema.yaml",
            {"catalogs": [{"match": "d/**/*.mmd"}], "diagrams": [{"match": "d/**", "validation": "strict"}]},
        )
        _write_yaml(
            self.cdir / "archival-contract.schema.yaml",
            {"mode": "maybe", "eligibility": {"idle": {"field": "ctime"}, "status_in": "done"}},
        )
        found = vault_contracts.discover_contracts(self.vault)

        def fields(cid: str) -> set[tuple[str, str]]:
            return {(w["code"], w["field"]) for w in found[cid].get("warnings", [])}

        s = fields("search-contract")
        self.assertIn(("contract.invalid_shape", "default_exclude"), s)
        self.assertIn(("contract.invalid_shape", "folder_exclude[0].exclude"), s)
        g = fields("git-contract")
        self.assertIn(("contract.invalid_value", "host"), g)
        self.assertIn(("contract.invalid_value", "sync.debounce_seconds"), g)
        self.assertIn(("contract.unknown_key", "sync.push"), g)
        self.assertIn(("contract.invalid_shape", "remote"), g)
        t = fields("telemetry-contract")
        self.assertIn(("contract.invalid_value", "store.backend"), t)
        self.assertIn(("contract.invalid_value", "privacy.allow.paths"), t)
        m = fields("mermaid-contract")
        self.assertIn(("contract.invalid_shape", "catalogs[0].catalog_path"), m)
        self.assertIn(("contract.invalid_value", "diagrams[0].validation"), m)
        a = fields("archival-contract")
        self.assertIn(("contract.invalid_value", "mode"), a)
        self.assertIn(("contract.invalid_value", "eligibility.idle.field"), a)
        self.assertIn(("contract.invalid_shape", "eligibility.status_in"), a)

    def test_telemetry_backend_aliases_are_not_drift(self):
        # ``duckdb`` / ``local`` are historical spellings the backend resolver maps.
        self.assertEqual(
            vault_contracts.check_contract("telemetry-contract", {"store": {"backend": "duckdb"}}),
            [],
        )

    def test_unknown_ids_and_bad_data_are_safe(self):
        self.assertEqual(vault_contracts.check_contract("read-contract", {"x": 1}), [])
        self.assertEqual(vault_contracts.check_contract("okf-contract", None), [])
        self.assertEqual(vault_contracts.check_contract("okf-contract", ["not", "a", "map"]), [])

    def test_contract_fingerprint_tracks_every_contract(self):
        _write_yaml(self.cdir / "okf-contract.schema.yaml", {"okf_version": "0.1"})
        before = vault_contracts.contract_fingerprint(self.vault)
        self.assertIn("okf-contract.schema.yaml", before)
        _write_yaml(self.cdir / "table-contract.schema.yaml", {"tables": []})
        after = vault_contracts.contract_fingerprint(self.vault)
        self.assertNotEqual(before, after)
        self.assertIn("table-contract.schema.yaml", after)


class SharedYamlCacheTest(unittest.TestCase):
    def setUp(self):
        vault_contracts.clear_yaml_cache()
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-yc-"))
        self.path = self.tmp / "c.yaml"
        _write_yaml(self.path, {"a": [1, 2]})

    def tearDown(self):
        vault_contracts.clear_yaml_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parses_once_per_mtime_and_returns_copies(self):
        real = yaml.safe_load
        calls = {"n": 0}

        def counting(text):
            calls["n"] += 1
            return real(text)

        with mock.patch.object(vault_contracts.yaml, "safe_load", counting):
            first = vault_contracts.load_yaml_cached(self.path)
            second = vault_contracts.load_yaml_cached(self.path)
            self.assertEqual(calls["n"], 1)
            self.assertEqual(first, second)
            self.assertIsNot(first, second)
            first["mutated"] = True
            self.assertNotIn("mutated", vault_contracts.load_yaml_cached(self.path))
            _write_yaml(self.path, {"a": [3]})
            st = self.path.stat()
            os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
            self.assertEqual(vault_contracts.load_yaml_cached(self.path), {"a": [3]})
            self.assertEqual(calls["n"], 2)

    def test_non_mapping_and_missing(self):
        _write(self.path, "- just\n- a list\n")
        self.assertIsNone(vault_contracts.load_yaml_cached(self.path))
        self.assertIsNone(vault_contracts.load_yaml_cached(self.tmp / "missing.yaml"))


# ------------------------------------------------------------------ lint parity


_LINT_CONTRACT = """
okf_version: "0.1"
default_okf_type: Note
default_enforcement: soft
core_required: [okf_type, description, timestamp]
type_field: okf_type
reserved_filenames: [index.md, log.md]
path_rules:
  - match: "index.md"
    enforcement: exempt
  - match: "**/index.md"
    enforcement: reserved
  - match: "areas/threads/**"
    okf_type: Thread
    enforcement: soft
    required_fields: [okf_type, description, timestamp, title]
  - match: "projects/hard/**"
    okf_type: Project
    enforcement: hard
provenance_optional: [sources]
"""


class VaultLintContractParityTest(unittest.TestCase):
    def setUp(self):
        okf.clear_contract_cache()
        vault_contracts.clear_yaml_cache()
        self._env = {}
        for key in (
            "APO_OKF_CONTRACT",
            "APO_OKF_ENFORCEMENT",
            "APO_NOTES_ROOT",
            "APO_COLLECTION",
            "APO_INDEX",
            "APO_VAULTS",
            "APO_COLLECTION_ROOT",
            "APO_VAULT_PATHS",
            "APO_DEFAULT_VAULT",
        ):
            self._env[key] = os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _write(self.root / "system/contracts/okf-contract.schema.yaml", _LINT_CONTRACT)
        _write(self.root / "index.md", "---\nokf_version: '0.1'\n---\n# Root\n")
        # missing title (rule-required) + description
        _write(
            self.root / "areas/threads/t1.md",
            "---\nokf_type: Thread\ntimestamp: '2026-01-01T00:00:00Z'\n---\n\nbody\n",
        )
        # conformant
        _write(
            self.root / "areas/threads/t2.md",
            "---\nokf_type: Thread\ntitle: T2\ndescription: d\ntimestamp: '2026-01-01T00:00:00Z'\n---\n\nbody\n",
        )
        # no frontmatter at all
        _write(self.root / "resources/bare.md", "# Bare\n\ntext\n")
        # reserved path carrying concept frontmatter
        _write(self.root / "projects/p/index.md", "---\nokf_type: Note\n---\n# P\n")
        # hard path missing description
        _write(
            self.root / "projects/hard/h.md",
            "---\nokf_type: Project\ntimestamp: '2026-01-01T00:00:00Z'\n---\n\nbody\n",
        )
        self._cfg = {k: getattr(config, k) for k in ("NOTES_ROOT", "INDEX_PATH", "COLLECTION")}
        config.NOTES_ROOT = self.root.resolve()
        config.INDEX_PATH = (self.root / "index.db").resolve()
        config.COLLECTION = "lint_parity_test"
        ops._lint_sweep_cache.clear()

    def tearDown(self):
        okf.clear_contract_cache()
        vault_contracts.clear_yaml_cache()
        ops._lint_sweep_cache.clear()
        for k, val in self._cfg.items():
            setattr(config, k, val)
        self.tmp.cleanup()
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def _lint_flaws(self, **kw) -> list[dict]:
        out = ops.vault_op("lint", limit=500, **kw)
        self.assertTrue(out["ok"], out)
        return out["flaws"]

    def test_lint_reports_okf_producer_violations(self):
        flaws = self._lint_flaws()
        by_path: dict[str, set[str]] = {}
        for f in flaws:
            by_path.setdefault(f["path"], set()).add(f["code"])
        self.assertIn("okf.missing_field", by_path["areas/threads/t1.md"])
        self.assertNotIn("areas/threads/t2.md", by_path)
        self.assertIn("okf.missing_frontmatter", by_path["resources/bare.md"])
        self.assertIn("okf.reserved_frontmatter", by_path["projects/p/index.md"])
        self.assertIn("okf.missing_field", by_path["projects/hard/h.md"])
        # Bundle-root index.md is exempt.
        self.assertNotIn("index.md", by_path)
        t1 = [f for f in flaws if f["path"] == "areas/threads/t1.md" and f["code"] == "okf.missing_field"]
        fields = {f["evidence"]["field"] for f in t1}
        self.assertEqual(fields, {"description", "title"})
        self.assertTrue(all(f["suggested_op"]["ops"][0]["op"] == "set_field" for f in t1))
        hard = next(f for f in flaws if f["path"] == "projects/hard/h.md")
        self.assertEqual(hard["severity"], "error")

    def test_lint_matches_cli_validate(self):
        lint_paths = {
            f["path"]
            for f in self._lint_flaws()
            if f["code"].startswith("okf.")
        }
        summary = okf_cli.validate_vault(self.root, profile="apo")
        cli_paths = {v["path"] for v in summary.violations}
        self.assertEqual(lint_paths, cli_paths)

    def test_lint_reports_contract_drift_only_unscoped(self):
        flaws = self._lint_flaws()
        drift = [f for f in flaws if f["code"] == "contract.unknown_key"]
        self.assertEqual(len(drift), 1, drift)
        self.assertEqual(drift[0]["path"], "system/contracts/okf-contract.schema.yaml")
        self.assertEqual(drift[0]["evidence"]["field"], "provenance_optional")
        self.assertEqual(drift[0]["remediation"], "human")
        scoped = self._lint_flaws(folder="areas/threads")
        self.assertFalse(any(f["code"].startswith("contract.") for f in scoped))
        self.assertTrue(any(f["code"] == "okf.missing_field" for f in scoped))

    def test_lint_reports_unreadable_contract(self):
        _write(self.root / "system/contracts/table-contract.schema.yaml", ":\n  - bad\n")
        ops._lint_sweep_cache.clear()
        flaws = self._lint_flaws()
        bad = [f for f in flaws if f["code"] == "contract.unreadable"]
        self.assertEqual(len(bad), 1, bad)
        self.assertEqual(bad[0]["severity"], "error")
        self.assertEqual(bad[0]["path"], "system/contracts/table-contract.schema.yaml")

    def test_contract_edit_misses_lint_cache(self):
        self.assertTrue(any(f["code"] == "contract.unknown_key" for f in self._lint_flaws()))
        cpath = self.root / "system/contracts/okf-contract.schema.yaml"
        _write(cpath, _LINT_CONTRACT.replace("provenance_optional: [sources]\n", ""))
        st = cpath.stat()
        os.utime(cpath, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        okf.clear_contract_cache()
        self.assertFalse(any(f["code"] == "contract.unknown_key" for f in self._lint_flaws()))

    def test_no_contract_no_okf_flaws(self):
        (self.root / "system/contracts/okf-contract.schema.yaml").unlink()
        okf.clear_contract_cache()
        ops._lint_sweep_cache.clear()
        flaws = self._lint_flaws()
        self.assertFalse(any(f["code"].startswith("okf.") for f in flaws), flaws)

    def test_lint_note_include_okf_switch(self):
        text = (self.root / "areas/threads/t1.md").read_text(encoding="utf-8")
        _, with_okf = note_lint.lint_note(text, path="areas/threads/t1.md", vault_root=self.root)
        _, without = note_lint.lint_note(
            text, path="areas/threads/t1.md", vault_root=self.root, include_okf=False
        )
        self.assertTrue(any(f["code"] == "okf.missing_field" for f in with_okf))
        self.assertFalse(any(f["code"].startswith("okf.") for f in without))


class SearchContractCheckUnitTest(unittest.TestCase):
    def test_folder_exclude_rule_shape(self):
        found = search_contract.check_contract(
            {
                "search_contract_version": "0.1",
                "default_exclude": ["archives/*"],
                "folder_exclude": [
                    {"folder": "areas/threads", "exclude": ["areas/threads/x.md"], "unless_query": ["x"]},
                    {"exclude": ["y"]},
                    "not-a-rule",
                ],
                "ranking": {},
            }
        )
        codes = {(f["code"], f["field"]) for f in found}
        self.assertEqual(
            codes,
            {
                ("contract.unknown_key", "ranking"),
                ("contract.invalid_shape", "folder_exclude[1].folder"),
                ("contract.invalid_shape", "folder_exclude[2]"),
            },
        )


if __name__ == "__main__":
    unittest.main()
