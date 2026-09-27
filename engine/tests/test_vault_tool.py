"""vault tool — list / contracts / describe + system/contracts discovery."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from apo_engine import ops, vault_contracts, vault_project


class ContractIdTest(unittest.TestCase):
    def test_id_from_schema_yaml(self):
        self.assertEqual(
            vault_contracts.contract_id_from_name("okf-contract.schema.yaml"),
            "okf-contract",
        )
        self.assertEqual(
            vault_contracts.contract_id_from_name("usage.yaml"),
            "usage",
        )


class IntegrationsFormatTest(unittest.TestCase):
    def test_str_list_accepts_scalar_and_list(self):
        self.assertEqual(vault_project._str_list("apo"), ["apo"])
        self.assertEqual(vault_project._str_list(["apo", " context7 "]), ["apo", "context7"])
        self.assertEqual(vault_project._str_list(""), [])
        self.assertEqual(vault_project._str_list(None), [])

    def test_format_integrations_scalar_required(self):
        line = vault_project.format_integrations_line(
            "meta",
            {"mcp": {"required": "apo", "expected": ["context7"]}, "cli": "gws"},
        )
        self.assertIn("required=`apo`", line)
        self.assertIn("expected=`context7`", line)
        self.assertIn("cli=`gws`", line)


class WriteHabitsProjectTest(unittest.TestCase):
    def test_normalize_note_types_empty_uses_floor(self):
        listed, warnings = vault_project.normalize_note_types(None)
        self.assertEqual(listed, [".md", ".mmd", ".yaml", ".yml"])
        self.assertEqual(warnings, [])

    def test_normalize_note_types_clamps_superset(self):
        listed, warnings = vault_project.normalize_note_types([".md", ".json", ".py"])
        self.assertEqual(listed, [".md"])
        self.assertTrue(any(".json" in w for w in warnings))
        self.assertTrue(any(".py" in w for w in warnings))

    def test_format_contribution_line_includes_note_types(self):
        line = vault_project.format_contribution_line(
            "work",
            {"dialect": "obsidian-ofm", "note_types": [".md", ".yaml"]},
        )
        self.assertIn("note_types=", line)
        self.assertIn(".md", line)
        self.assertIn(".yaml", line)

    def test_render_desk_body_read_only_column_and_note_types(self):
        merge = {
            "default_vault": "work",
            "vaults": {
                "work": {
                    "root": "/vault/work",
                    "default": True,
                    "read_only": False,
                    "contracts": {
                        "usage-contract": {
                            "ok": True,
                            "data": {
                                "contribution": {
                                    "dialect": "gfm",
                                    "note_types": [".md"],
                                },
                                "write_habits": ["mutator_note_types_only"],
                            },
                        },
                    },
                },
                "compliance": {
                    "root": "/vault/compliance",
                    "read_only": True,
                    "contracts": {},
                },
            },
            "desk": {"habits": {}},
        }
        body = vault_project.render_desk_body(merge)
        self.assertIn("Read-only", body)
        self.assertIn("| `compliance`", body)
        self.assertIn("| yes |", body)
        self.assertIn("note_types=", body)
        self.assertIn("Mutators (`write_note`", body)

    def test_render_write_habit_lines_known_ids(self):
        lines = vault_project._render_write_habit_lines(
            [
                ("folder_on_search", None),
                ("patch_note_wire", None),
                ("vault_api_routing", None),
            ]
        )
        self.assertEqual(len(lines), 3)
        self.assertIn("folder=", lines[0])
        self.assertIn("patch_note", lines[1])
        self.assertIn("request={action:", lines[2])

    def test_render_write_habit_lines_dialect_specific_ids(self):
        lines = vault_project._render_write_habit_lines(
            [("task_router_threads", None), ("filter_memory_type", None)]
        )
        self.assertEqual(len(lines), 2)
        self.assertIn("areas/threads/", lines[0])
        self.assertIn("memory_type", lines[1])

    def test_render_write_habit_lines_inline_text_overrides_dict(self):
        lines = vault_project._render_write_habit_lines(
            [("custom_habit", "Do the custom thing.")]
        )
        self.assertEqual(lines, ["- **`custom_habit`:** Do the custom thing."])

    def test_render_write_habit_lines_unknown_id_falls_back(self):
        lines = vault_project._render_write_habit_lines([("totally_unknown_id", None)])
        self.assertEqual(
            lines, ["- `totally_unknown_id` — see usage-contract / apo-write-api."]
        )

    def test_usage_write_habits_parses_str_and_inline_object(self):
        row = {
            "contracts": {
                "usage-contract": {
                    "ok": True,
                    "data": {
                        "write_habits": [
                            "folder_on_search",
                            {"id": "custom_habit", "text": "Do the custom thing."},
                        ]
                    },
                }
            }
        }
        habits = vault_project._usage_write_habits(row)
        self.assertEqual(
            habits, [("folder_on_search", None), ("custom_habit", "Do the custom thing.")]
        )

    def test_render_desk_body_includes_apo_throughput(self):
        merge = {
            "default_vault": "meta",
            "vaults": {
                "meta": {
                    "root": "/Users/jnorris/Notes/Atlas",
                    "default": True,
                    "contracts": {
                        "usage-contract": {
                            "ok": True,
                            "data": {
                                "write_habits": [
                                    "folder_on_search",
                                    "dedupe_search",
                                ],
                            },
                        },
                    },
                },
            },
            "desk": {"habits": {}},
        }
        body = vault_project.render_desk_body(merge)
        self.assertIn("## Apo throughput", body)
        self.assertIn("Hard gate", body)
        self.assertIn("**One** `search_notes`", body)

    def test_render_desk_body_flags_missing_desk_yaml(self):
        merge = {
            "default_vault": "meta",
            "vaults": {"meta": {"root": "/tmp/x", "default": True, "contracts": {}}},
            "desk": {"habits": {}},
            "desk_meta": {"source": "defaults", "path": None},
        }
        body = vault_project.render_desk_body(merge)
        self.assertIn("No `~/.apo/desk.yaml` found", body)

    def test_render_desk_body_omits_missing_desk_yaml_note_when_configured(self):
        merge = {
            "default_vault": "meta",
            "vaults": {"meta": {"root": "/tmp/x", "default": True, "contracts": {}}},
            "desk": {"habits": {}},
            "desk_meta": {"source": "file", "path": "/home/jeremy/.apo/desk.yaml"},
        }
        body = vault_project.render_desk_body(merge)
        self.assertNotIn("No `~/.apo/desk.yaml` found", body)

    def test_render_desk_body_surfaces_previously_dropped_contract_fields(self):
        merge = {
            "default_vault": "atlas",
            "vaults": {
                "atlas": {
                    "root": "/vault/atlas",
                    "default": True,
                    "contracts": {
                        "usage-contract": {
                            "ok": True,
                            "data": {
                                "purpose": "Personal PKB. Second sentence.",
                                "in_scope": ["areas/", "projects/"],
                                "out_scope": ["household content"],
                                "layout": {"areas": "standing responsibilities"},
                                "frontmatter_floor": ["title", "okf_type"],
                                "consult_vault_first": "Search this vault before asking.",
                                "task_routing": "Route tasks through areas/threads/.",
                                "token_budget": 800,
                            },
                        },
                        "okf-contract": {
                            "ok": True,
                            "data": {
                                "path_rules": [
                                    {
                                        "match": "areas/threads/**",
                                        "enforcement": "soft",
                                        "okf_type": "Thread",
                                        "required_fields": ["okf_type", "timestamp"],
                                    }
                                ]
                            },
                        },
                        "git-contract": {
                            "ok": True,
                            "data": {
                                "never_commit": [".env"],
                                "sync": {"on_block_command": "notify-send blocked"},
                                "restore": {"owner": "jeremy", "drill": "quarterly"},
                            },
                        },
                        "telemetry-contract": {
                            "ok": True,
                            "data": {
                                "privacy": {
                                    "allow": {"dimensions": ["path"]},
                                    "deny": ["body"],
                                },
                                "retention_days": 30,
                                "agent_access": {"expose_paths": ["system/audit"]},
                            },
                        },
                        "local-web-contract": {
                            "ok": True,
                            "data": {"bind": "127.0.0.1", "port": 7432, "mode": "adaptive"},
                        },
                    },
                },
            },
            "desk": {"habits": {}},
        }
        body = vault_project.render_desk_body(merge)
        self.assertIn("## Vault purpose & scope", body)
        self.assertIn("Personal PKB", body)
        self.assertIn("in scope: areas/; projects/", body)
        self.assertIn("## Folder layout", body)
        self.assertIn("standing responsibilities", body)
        self.assertIn("## Frontmatter floor", body)
        self.assertIn("`title`, `okf_type`", body)
        self.assertIn("## Vault directives", body)
        self.assertIn("Search this vault before asking.", body)
        self.assertIn("Route tasks through areas/threads/.", body)
        self.assertIn("## Type routing (OKF)", body)
        self.assertIn("`areas/threads/**`", body)
        self.assertIn("Thread", body)
        self.assertIn("## Git safety", body)
        self.assertIn("`.env`", body)
        self.assertIn("notify-send blocked", body)
        self.assertIn("## Telemetry privacy", body)
        self.assertIn("retention=30d", body)
        self.assertIn("## Local web browser", body)
        self.assertIn("127.0.0.1:7432", body)

    def test_render_desk_index_is_compact_and_carries_tool_call_directive(self):
        merge = {
            "default_vault": "atlas",
            "vaults": {
                "atlas": {
                    "root": "/vault/atlas",
                    "default": True,
                    "role": "personal PARA",
                    "contract_ids": ["usage-contract", "okf-contract"],
                    "contracts": {
                        "usage-contract": {
                            "ok": True,
                            "data": {
                                "purpose": "Personal PKB.",
                                "frontmatter_floor": ["title", "okf_type"],
                            },
                        },
                    },
                },
            },
            "desk": {"habits": {}},
        }
        index_body = vault_project.render_desk_index(merge)
        full_body = vault_project.render_desk_body(merge)
        self.assertLess(len(index_body.encode("utf-8")), 2000)
        self.assertLess(len(index_body), len(full_body))
        # The tool-call directive is the load-bearing line — must be present
        # and front-loaded (before the vault table), not buried.
        directive_pos = index_body.find('vault(request={"action": "project", "vaults": ["<id>"]})')
        table_pos = index_body.find("## Desk vaults")
        self.assertGreater(directive_pos, -1)
        self.assertGreater(table_pos, -1)
        self.assertLess(directive_pos, table_pos)
        self.assertIn('vault(request={"action": "stats"', index_body)
        self.assertIn("`atlas`", index_body)
        self.assertIn("personal PARA", index_body)
        # Sections that belong to Tier 2 only, not the compact index.
        self.assertNotIn("## Frontmatter floor", index_body)
        self.assertNotIn("## Vault purpose & scope", index_body)
        self.assertNotIn("## Type routing (OKF)", index_body)
        self.assertNotIn("## Write workflow", index_body)
        self.assertNotIn("## Examples", index_body)

    def test_project_mode_index_vs_full(self):
        merge = {
            "default_vault": "atlas",
            "vaults": {"atlas": {"root": "/vault/atlas", "default": True, "contracts": {}}},
            "desk": {"habits": {}},
        }
        full = vault_project.project(merge)
        indexed = vault_project.project(merge, mode="index")
        self.assertEqual(full["mode"], "full")
        self.assertEqual(indexed["mode"], "index")
        self.assertLess(indexed["bytes"], full["bytes"])
        self.assertEqual(full["body"], vault_project.render_desk_body(merge))
        self.assertEqual(indexed["body"], vault_project.render_desk_index(merge))

    def test_render_desk_body_includes_write_workflow_section(self):
        merge = {
            "default_vault": "atlas",
            "vaults": {"atlas": {"root": "/vault/atlas", "default": True, "contracts": {}}},
            "desk": {"habits": {}},
        }
        body = vault_project.render_desk_body(merge)
        self.assertIn("## Write workflow", body)
        self.assertIn("search_notes", body)
        self.assertIn("expected_mtime", body)
        # Sequential, not just enumerable — steps are numbered in order.
        self.assertLess(body.index("1. `search_notes`"), body.index("4. On any follow-up"))

    def test_render_desk_body_omits_examples_section(self):
        merge = {
            "default_vault": "atlas",
            "vaults": {"atlas": {"root": "/vault/atlas", "default": True, "contracts": {}}},
            "desk": {"habits": {}},
        }
        body = vault_project.render_desk_body(merge)
        self.assertNotIn("## Examples", body)
        self.assertNotIn("<example>", body)

    def test_render_desk_body_omits_contract_inventory(self):
        merge = {
            "default_vault": "atlas",
            "vaults": {
                "atlas": {
                    "root": "/vault/atlas",
                    "default": True,
                    "contracts": {
                        "usage-contract": {"ok": True, "data": {}},
                    },
                },
            },
            "desk": {"habits": {}},
        }
        body = vault_project.render_desk_body(merge)
        self.assertNotIn("## Contract inventory", body)

    def test_render_desk_body_front_loads_purpose_scope_and_floor(self):
        merge = {
            "default_vault": "atlas",
            "vaults": {
                "atlas": {
                    "root": "/vault/atlas",
                    "default": True,
                    "contracts": {
                        "usage-contract": {
                            "ok": True,
                            "data": {
                                "purpose": "Personal PKB.",
                                "frontmatter_floor": ["title", "okf_type"],
                                "contribution": {"dialect": "obsidian-ofm"},
                            },
                        },
                    },
                },
            },
            "desk": {"habits": {}},
        }
        body = vault_project.render_desk_body(merge)
        desk_pos = body.index("## Desk vaults")
        scope_pos = body.index("## Vault purpose & scope")
        floor_pos = body.index("## Frontmatter floor")
        contrib_pos = body.index("## Contribution")
        self.assertLess(desk_pos, scope_pos)
        self.assertLess(scope_pos, floor_pos)
        self.assertLess(floor_pos, contrib_pos)

    def test_render_desk_body_wraps_directives_and_workflow_in_delimiters(self):
        merge = {
            "default_vault": "atlas",
            "vaults": {
                "atlas": {
                    "root": "/vault/atlas",
                    "default": True,
                    "contracts": {
                        "usage-contract": {
                            "ok": True,
                            "data": {"consult_vault_first": "Search first."},
                        },
                    },
                },
            },
            "desk": {"habits": {}},
        }
        body = vault_project.render_desk_body(merge)
        open_pos = body.index("<vault-directives>")
        close_pos = body.index("</vault-directives>")
        directives_pos = body.index("## Vault directives")
        workflow_pos = body.index("## Write workflow")
        self.assertLess(open_pos, directives_pos)
        self.assertLess(directives_pos, workflow_pos)
        self.assertLess(workflow_pos, close_pos)

    def test_render_desk_body_ends_with_safety(self):
        merge = {
            "default_vault": "atlas",
            "vaults": {
                "atlas": {
                    "root": "/vault/atlas",
                    "default": True,
                    "contracts": {
                        "usage-contract": {
                            "ok": True,
                            "data": {
                                "consult_vault_first": "Search first.",
                                "task_routing": "Route via threads.",
                            },
                        },
                    },
                },
            },
            "desk": {"habits": {}},
        }
        body = vault_project.render_desk_body(merge)
        self.assertNotIn("## Key directives (recap)", body)
        self.assertIn("## Safety", body)
        self.assertIn("Search first.", body.split("## Vault directives")[1])
        self.assertTrue(body.rstrip().endswith("Calendar."))

    def test_format_okf_path_rules_lines_truncates_to_token_budget(self):
        okf = {
            "path_rules": [
                {"match": f"path{i}/**", "enforcement": "soft", "okf_type": "Note"}
                for i in range(60)
            ]
        }
        lines = vault_project.format_okf_path_rules_lines("atlas", okf, token_budget=20)
        self.assertLess(len(lines), 60)
        self.assertTrue(any("more — see okf-contract" in line for line in lines))


class ReadContractProjectTest(unittest.TestCase):
    """read-contract: format_read_routing_lines + render_desk_body wiring.

    Additive over okf-contract — see projects/apo-pkb/read-contract-consumer-side.md
    (atlas, via the apo MCP) for the design. No engine discovery/merge changes were
    needed (vault_contracts.discover_contracts is generic over filenames); this
    covers only the new projection formatter + its render_desk_body wiring.
    """

    def _read_data(self) -> dict:
        return {
            "read_contract_version": "0.1",
            "type_field": "okf_type",
            "type_authority": {
                "Obligation": {
                    "authority": "source_of_truth",
                    "verify": True,
                    "verify_hint": "cross-check status + cadence against today",
                },
                "System": {"authority": "authoritative", "verify": False},
            },
            "purposes": {
                "compliance": {
                    "entry": {
                        "okf_type": "Obligation",
                        "where": {"status": {"$in": ["active", "pending"]}},
                        "order_by": "cadence",
                    },
                    "read_order": ["Obligation", "Review", "Policy"],
                },
            },
        }

    def test_format_read_routing_lines_basic(self):
        lines = vault_project.format_read_routing_lines(
            "atlas", self._read_data(), token_budget=None
        )
        text = "\n".join(lines)
        self.assertIn("### `atlas`", text)
        self.assertIn("`compliance`", text)
        self.assertIn("`Obligation`", text)
        self.assertIn('"status"', text)
        self.assertIn("`Obligation` → `Review` → `Policy`", text)
        self.assertIn("Type authority:", text)
        self.assertIn("`Obligation`=source_of_truth", text)
        self.assertIn("cross-check status + cadence against today", text)
        self.assertIn("`System`=authoritative", text)

    def test_format_read_routing_lines_empty_purposes_is_noop(self):
        self.assertEqual(vault_project.format_read_routing_lines("atlas", {}, token_budget=None), [])
        self.assertEqual(
            vault_project.format_read_routing_lines("atlas", {"purposes": {}}, token_budget=None),
            [],
        )

    def test_format_read_routing_lines_truncates_to_token_budget(self):
        read = {
            "purposes": {
                f"purpose{i}": {"entry": {"okf_type": "Note"}, "read_order": ["Note"]}
                for i in range(60)
            }
        }
        lines = vault_project.format_read_routing_lines("atlas", read, token_budget=20)
        self.assertLess(len(lines), 60)
        self.assertTrue(any("more — see read-contract" in line for line in lines))

    def test_render_desk_body_includes_read_routing_section(self):
        merge = {
            "default_vault": "atlas",
            "vaults": {
                "atlas": {
                    "root": "/vault/atlas",
                    "default": True,
                    "contracts": {
                        "okf-contract": {
                            "ok": True,
                            "data": {
                                "path_rules": [
                                    {
                                        "match": "areas/compliance/**/obl-*.md",
                                        "enforcement": "soft",
                                        "okf_type": "Obligation",
                                    }
                                ]
                            },
                        },
                        "read-contract": {"ok": True, "data": self._read_data()},
                    },
                },
            },
            "desk": {"habits": {}},
        }
        body = vault_project.render_desk_body(merge)
        self.assertIn("## Read routing", body)
        self.assertIn("`compliance`", body)
        read_pos = body.index("## Read routing")
        okf_pos = body.index("## Type routing (OKF)")
        session_pos = body.index("## Session audit")
        self.assertLess(okf_pos, read_pos)
        self.assertLess(read_pos, session_pos)

    def test_render_desk_body_without_read_contract_is_byte_identical(self):
        """Regression: a vault with no read-contract renders exactly as it did
        before this feature existed — same merge minus the read-contract entry
        produces a body with the Read routing block removed and nothing else
        changed."""
        base_contracts = {
            "usage-contract": {
                "ok": True,
                "data": {"purpose": "Personal PKB.", "token_budget": 800},
            },
            "okf-contract": {
                "ok": True,
                "data": {
                    "path_rules": [
                        {
                            "match": "areas/compliance/**/obl-*.md",
                            "enforcement": "soft",
                            "okf_type": "Obligation",
                        }
                    ]
                },
            },
        }
        merge_without = {
            "default_vault": "atlas",
            "vaults": {"atlas": {"root": "/vault/atlas", "default": True, "contracts": base_contracts}},
            "desk": {"habits": {}},
        }
        merge_with = {
            "default_vault": "atlas",
            "vaults": {
                "atlas": {
                    "root": "/vault/atlas",
                    "default": True,
                    "contracts": {
                        **base_contracts,
                        "read-contract": {"ok": True, "data": self._read_data()},
                    },
                }
            },
            "desk": {"habits": {}},
        }
        body_without = vault_project.render_desk_body(merge_without)
        body_with = vault_project.render_desk_body(merge_with)
        self.assertNotIn("## Read routing", body_without)
        self.assertIn("## Read routing", body_with)
        read_block_start = body_with.index("## Read routing")
        read_block_end = body_with.index("## Session audit")
        stripped = body_with[:read_block_start] + body_with[read_block_end:]
        self.assertEqual(stripped, body_without)

    def test_render_desk_body_read_contract_deterministic_regardless_of_key_order(self):
        """Same vault + same read-contract data -> byte-identical projection,
        independent of the YAML mapping's key insertion order (a real vault
        author may reorder purposes/type_authority keys without meaning to
        change anything semantically)."""
        data_a = self._read_data()
        # Same content, different insertion order for both dict-valued sections.
        data_b = {
            "read_contract_version": data_a["read_contract_version"],
            "type_field": data_a["type_field"],
            "purposes": dict(data_a["purposes"]),
            "type_authority": dict(reversed(list(data_a["type_authority"].items()))),
        }

        def _merge(data: dict) -> dict:
            return {
                "default_vault": "atlas",
                "vaults": {
                    "atlas": {
                        "root": "/vault/atlas",
                        "default": True,
                        "contracts": {"read-contract": {"ok": True, "data": data}},
                    }
                },
                "desk": {"habits": {}},
            }

        body_a = vault_project.render_desk_body(_merge(data_a))
        body_b = vault_project.render_desk_body(_merge(data_b))
        self.assertEqual(body_a, body_b)
        # And calling again with the same input is itself byte-identical.
        self.assertEqual(body_a, vault_project.render_desk_body(_merge(data_a)))


class DiscoverContractsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-vcontracts-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prefers_system_contracts_over_legacy(self):
        legacy = self.vault / "system" / "config"
        legacy.mkdir(parents=True)
        (legacy / "okf-contract.schema.yaml").write_text(
            "okf_version: '0.1'\nfrom: legacy\n", encoding="utf-8"
        )
        preferred = self.vault / "system" / "contracts"
        preferred.mkdir(parents=True)
        (preferred / "okf-contract.schema.yaml").write_text(
            "okf_version: '0.1'\nfrom: contracts\n", encoding="utf-8"
        )
        found = vault_contracts.discover_contracts(self.vault)
        self.assertIn("okf-contract", found)
        self.assertEqual(found["okf-contract"]["source"], "contracts")
        self.assertEqual(found["okf-contract"]["data"].get("from"), "contracts")

    def test_legacy_git_and_okf_profile(self):
        cfg = self.vault / "system" / "config"
        cfg.mkdir(parents=True)
        (cfg / "git-contract.schema.yaml").write_text(
            "git_contract_version: '0.1'\nremote: 'https://x.git'\n",
            encoding="utf-8",
        )
        (cfg / "okf-profile.schema.yaml").write_text(
            "okf_version: '0.1'\n", encoding="utf-8"
        )
        found = vault_contracts.discover_contracts(self.vault)
        self.assertEqual(set(found), {"git-contract", "okf-profile"})
        self.assertTrue(all(e["source"] == "legacy" for e in found.values()))
        self.assertTrue(all(e["ok"] for e in found.values()))

    def test_invalid_yaml_marks_entry_not_ok(self):
        cdir = self.vault / "system" / "contracts"
        cdir.mkdir(parents=True)
        (cdir / "broken.schema.yaml").write_text(":\n  - bad\n", encoding="utf-8")
        found = vault_contracts.discover_contracts(self.vault)
        self.assertFalse(found["broken"]["ok"])
        self.assertIn("error", found["broken"])


class VaultOpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-vault-op-"))
        self.a = self.tmp / "a"
        self.b = self.tmp / "b"
        self.a.mkdir()
        self.b.mkdir()
        (self.a / "inbox").mkdir()
        cdir = self.a / "system" / "contracts"
        cdir.mkdir(parents=True)
        (cdir / "usage-contract.schema.yaml").write_text(
            "usage_contract_version: '0.1'\n"
            "vault_id: alpha\n"
            "purpose: test\n"
            "pointers:\n"
            "  - alpha:system/config/agent-memory-policy\n"
            "contribution:\n"
            "  dialect: obsidian-ofm\n"
            "  features:\n"
            "    callouts: preferred\n"
            "  surfaces:\n"
            "    session_log:\n"
            "      dialect: gfm\n"
            "      callouts: never\n"
            "  render:\n"
            "    profile: htmlize\n"
            "  pointers:\n"
            "    - alpha:system/config/obsidian-callouts\n"
            "integrations:\n"
            "  mcp:\n"
            "    required: [apo]\n"
            "    expected: [context7]\n"
            "    optional: [granola]\n"
            "    never: [slack]\n"
            "    proxy:\n"
            "      name: lazy-mcp\n"
            "      via: [bugsnag, gtm]\n"
            "      parked: [metabase, stape]\n"
            "      catalog: alpha:system/config/lazy-mcp/servers.json\n"
            "      enable_parked: Flip enabled in catalog then SIGHUP\n"
            "  cli:\n"
            "    expected: [gws]\n"
            "  pointers:\n"
            "    - alpha:system/config/source-routing\n",
            encoding="utf-8",
        )
        cfg = self.b / "system" / "config"
        cfg.mkdir(parents=True)
        (cfg / "git-contract.schema.yaml").write_text(
            "git_contract_version: '0.1'\n"
            "remote: 'https://b.git'\n"
            "never_commit: ['.env']\n"
            "sync:\n"
            "  on_block_command: notify-send blocked\n",
            encoding="utf-8",
        )
        b_contracts = self.b / "system" / "contracts"
        b_contracts.mkdir(parents=True, exist_ok=True)
        (b_contracts / "usage-contract.schema.yaml").write_text(
            "usage_contract_version: '0.1'\nvault_id: beta\npurpose: beta\n",
            encoding="utf-8",
        )
        registry = {
            "default": "alpha",
            "vaults": {
                "alpha": {
                    "root": str(self.a),
                    "index": str(self.tmp / "a.db"),
                    "collection": "alpha",
                },
                "beta": {
                    "root": str(self.b),
                    "index": str(self.tmp / "b.db"),
                    "collection": "beta",
                },
            },
        }
        self.reg_path = self.tmp / "vaults.json"
        self.reg_path.write_text(json.dumps(registry), encoding="utf-8")
        self._env = unittest.mock.patch.dict(
            os.environ,
            {
                "APO_VAULTS": str(self.reg_path),
                "APO_NOTES_ROOT": str(self.a),
                "APO_INDEX": str(self.tmp / "legacy.db"),
                "APO_COLLECTION": "legacy",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list(self):
        out = ops.vault_op("list")
        self.assertTrue(out["ok"])
        self.assertEqual(out["default_vault"], "alpha")
        self.assertEqual(set(out["vaults"]), {"alpha", "beta"})
        self.assertTrue(out["vaults"]["alpha"]["default"])
        self.assertIn("inbox", out["vaults"]["alpha"]["top_level_dirs"])
        ids = {c["id"] for c in out["vaults"]["alpha"]["contracts"]}
        self.assertEqual(ids, {"usage-contract"})
        ids_b = {c["id"] for c in out["vaults"]["beta"]["contracts"]}
        self.assertEqual(ids_b, {"git-contract", "usage-contract"})

    def test_contracts_all_and_one(self):
        all_out = ops.vault_op("contracts")
        self.assertTrue(all_out["ok"])
        self.assertFalse(all_out["full"])
        self.assertIn("alpha", all_out["vaults"])
        alpha_c = all_out["vaults"]["alpha"]["contracts"]["usage-contract"]
        self.assertNotIn("data", alpha_c)
        self.assertEqual(alpha_c["path"], "system/contracts/usage-contract.schema.yaml")
        one = ops.vault_op("contracts", vault="beta")
        self.assertEqual(one["vault"], "beta")
        self.assertIn("git-contract", one["contracts"])
        self.assertNotIn("data", one["contracts"]["git-contract"])
        full = ops.vault_op("contracts", vault="beta", full=True)
        self.assertTrue(full["full"])
        self.assertEqual(
            full["contracts"]["git-contract"]["data"]["remote"],
            "https://b.git",
        )

    def test_describe_default(self):
        out = ops.vault_op("describe")
        self.assertTrue(out["ok"])
        self.assertEqual(out["vault"], "alpha")
        self.assertTrue(out["default"])
        self.assertEqual(out["contract_ids"], ["usage-contract"])
        self.assertNotIn("data", out["contracts"]["usage-contract"])
        full = ops.vault_op("describe", full=True)
        self.assertEqual(
            full["contracts"]["usage-contract"]["data"]["purpose"], "test"
        )

    def test_bad_action(self):
        out = ops.vault_op("explode")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_action")

    def test_clone_copies_system_files_not_content(self):
        # alpha already has system/contracts/usage-contract.schema.yaml; give it a
        # second system/ file plus real vault content that must NOT be cloned.
        (self.a / "system" / "config").mkdir(parents=True)
        (self.a / "system" / "config" / "notes.md").write_text("hi\n", encoding="utf-8")
        (self.a / "areas").mkdir(exist_ok=True)
        (self.a / "areas" / "secret.md").write_text("do not clone\n", encoding="utf-8")

        out = ops.vault_op("clone", vault="alpha", to="beta")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["from"], "alpha")
        self.assertEqual(out["to"], "beta")
        self.assertFalse(out["dry_run"])
        self.assertIn("system/config/notes.md", out["copied"])
        self.assertTrue(
            (self.b / "system" / "config" / "notes.md").is_file()
        )
        self.assertFalse((self.b / "areas" / "secret.md").exists())
        self.assertFalse((self.b / "areas").exists())

    def test_clone_skips_existing_destination_files(self):
        (self.b / "system" / "contracts").mkdir(parents=True, exist_ok=True)
        existing = self.b / "system" / "contracts" / "usage-contract.schema.yaml"
        existing.write_text("vault_id: beta\nuntouched: true\n", encoding="utf-8")

        out = ops.vault_op("clone", vault="alpha", to="beta")
        self.assertTrue(out["ok"], out)
        self.assertIn("system/contracts/usage-contract.schema.yaml", out["skipped_existing"])
        self.assertNotIn("system/contracts/usage-contract.schema.yaml", out["copied"])
        self.assertIn("untouched: true", existing.read_text(encoding="utf-8"))

    def test_clone_dry_run_writes_nothing(self):
        # beta already has its own usage-contract.schema.yaml (setUp), so that one
        # is a skip either way; add a file that's genuinely new to prove dry_run
        # reports it as "would copy" without actually writing it.
        (self.a / "system" / "config").mkdir(parents=True)
        (self.a / "system" / "config" / "notes.md").write_text("hi\n", encoding="utf-8")

        out = ops.vault_op("clone", vault="alpha", to="beta", dry_run=True)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["dry_run"])
        self.assertIn("system/config/notes.md", out["copied"])
        self.assertIn("system/contracts/usage-contract.schema.yaml", out["skipped_existing"])
        self.assertFalse((self.b / "system" / "config" / "notes.md").exists())

    def test_clone_requires_to(self):
        out = ops.vault_op("clone", vault="alpha")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_request")

    def test_clone_rejects_same_vault(self):
        out = ops.vault_op("clone", vault="alpha", to="alpha")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_request")

    def test_clone_unknown_destination(self):
        out = ops.vault_op("clone", vault="alpha", to="nope")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_vault")

    def test_vaults_filter_scopes_list(self):
        out = ops.vault_op("list", vaults=["alpha"])
        self.assertTrue(out["ok"])
        self.assertEqual(set(out["vaults"]), {"alpha"})
        self.assertEqual(out["default_vault"], "alpha")

    def test_vaults_filter_scopes_merge_and_project(self):
        merged = ops.vault_op("merge", vaults=["alpha"])
        self.assertTrue(merged["ok"])
        self.assertEqual(set(merged["vaults"]), {"alpha"})
        projected = ops.vault_op("project", vaults=["alpha"])
        self.assertTrue(projected["ok"])
        self.assertIn("`alpha`", projected["body"])
        self.assertNotIn("`beta`", projected["body"])

    def test_project_surfaces_non_usage_contract_data(self):
        # Regression: vault_op("project") used to merge with bodies=False and only
        # re-attach usage-contract data, silently dropping every other contract's
        # parsed body (git/okf/telemetry/search/local-web) before rendering.
        out = ops.vault_op("project")
        self.assertTrue(out["ok"])
        self.assertIn("## Git safety", out["body"])
        self.assertIn("`.env`", out["body"])
        self.assertIn("notify-send blocked", out["body"])

    def test_project_scopes_role_notes_and_pointers_to_active_vaults(self):
        desk = self.tmp / "desk-scope.yaml"
        desk.write_text(
            "desk_version: '0.1'\n"
            "citations: absolute_markdown\n"
            "vault_roles:\n"
            "  alpha: pkb\n"
            "  beta: employer\n"
            "role_notes:\n"
            "  pkb: 'Personal knowledge base only'\n"
            "  employer: 'GradGuard Work only'\n"
            "  household: 'Should never appear'\n"
            "pointers:\n"
            "  memory_policy: 'alpha:system/config/policy'\n"
            "  work_env: 'beta:system/config/work-environment'\n"
            "habits:\n"
            "  end_of_turn_gate: true\n",
            encoding="utf-8",
        )
        with unittest.mock.patch.dict(
            os.environ,
            {"APO_DESK_CONFIG": str(desk)},
        ):
            merged = ops.vault_op("merge", vaults=["alpha"])
            self.assertTrue(merged["ok"])
            desk_out = merged["desk"]
            self.assertEqual(set(desk_out.get("role_notes") or {}), {"pkb"})
            self.assertEqual(set(desk_out.get("pointers") or {}), {"memory_policy"})

            projected = ops.vault_op("project", vaults=["alpha"])
            self.assertTrue(projected["ok"])
            body = projected["body"]
            self.assertIn("Personal knowledge base only", body)
            self.assertNotIn("GradGuard Work only", body)
            self.assertNotIn("Should never appear", body)
            self.assertNotIn("work-environment", body)
            self.assertIn("policy", body)

    def test_project_mode_index_returns_compact_body(self):
        full = ops.vault_op("project", vaults=["alpha"])
        indexed = ops.vault_op("project", vaults=["alpha"], mode="index")
        self.assertTrue(full["ok"])
        self.assertTrue(indexed["ok"])
        self.assertEqual(full["mode"], "full")
        self.assertEqual(indexed["mode"], "index")
        self.assertLess(indexed["bytes"], full["bytes"])
        self.assertIn("`alpha`", indexed["body"])
        self.assertIn('vault(request={"action": "project", "vaults": ["<id>"]})', indexed["body"])
        self.assertIn('vault(request={"action": "stats"', indexed["body"])
        # default_vault/desk_meta are attached the same way regardless of mode.
        self.assertEqual(indexed["default_vault"], full["default_vault"])

    def test_project_mode_defaults_to_full_and_rejects_bad_value(self):
        default_mode = ops.vault_op("project", vaults=["alpha"])
        self.assertEqual(default_mode["mode"], "full")
        bad = ops.vault_op("project", vaults=["alpha"], mode="bogus")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"], "bad_request")

    def test_vaults_filter_reassigns_default_when_dropped(self):
        # beta is never the registry default (alpha is) — filtering to just
        # beta must not silently keep pointing "default" at the excluded alpha.
        out = ops.vault_op("list", vaults=["beta"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["default_vault"], "beta")
        self.assertTrue(out["vaults"]["beta"]["default"])

    def test_vaults_filter_scopes_describe_default(self):
        # No vault= given — describe's "default when empty" must resolve
        # against the filtered set, not the registry's true default (alpha).
        out = ops.vault_op("describe", vaults=["beta"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["vault"], "beta")

    def test_vaults_filter_rejects_unknown_name(self):
        out = ops.vault_op("list", vaults=["alpha", "ghost"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_vault")
        self.assertIn("ghost", out["message"])

    def test_vaults_filter_rejects_combo_with_vault(self):
        out = ops.vault_op("list", vault="alpha", vaults=["alpha", "beta"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_request")

    def test_project_returns_text(self):
        desk = self.tmp / "desk.yaml"
        desk.write_text(
            "desk_version: '0.1'\n"
            "citations: absolute_markdown\n"
            "workspace: '/tmp/desk.code-workspace'\n"
            "dual_write:\n"
            "  session_vault: sessions\n"
            "vault_roles:\n"
            "  alpha: pkb\n"
            "  beta: employer\n"
            "pointers:\n"
            "  memory_policy: 'alpha:system/config/policy'\n"
            "habits:\n"
            "  new_durable_facts: true\n",
            encoding="utf-8",
        )
        with unittest.mock.patch.dict(
            os.environ,
            {"APO_DESK_CONFIG": str(desk)},
        ):
            projected = ops.vault_op("project")
            self.assertTrue(projected["ok"])
            body = projected["body"]
            self.assertNotIn("suggested_paths", projected)
            self.assertNotIn("envelope", projected)
            self.assertNotIn("alwaysApply", body)
            self.assertIn("`alpha`", body)
            self.assertIn("pkb", body)
            self.assertIn("/tmp/desk.code-workspace", body)
            self.assertIn("Return-only", body)
            self.assertIn(str(self.a), body)
            self.assertIn("## Contribution", body)
            self.assertIn("obsidian-ofm", body)
            self.assertIn("## Expected integrations", body)
            self.assertIn("## MCP proxy", body)
            # root-level usage-contract pointers (e.g. memory policy) must not
            # be silently dropped from Deep policy pointers
            self.assertIn("agent-memory-policy", body)

            guidance = projected["guidance"]
            self.assertIn("Return-only", guidance)
            self.assertNotIn("cursor_mdc", guidance)
            self.assertGreater(projected["bytes"], 100)

    def test_merge_with_desk_file(self):
        desk = self.tmp / "desk.yaml"
        desk.write_text(
            "desk_version: '0.1'\n"
            "cross_pollinate_contracts: false\n"
            "citations: absolute_markdown\n"
            "dual_write:\n"
            "  session_vault: sessions\n"
            "vault_roles:\n"
            "  alpha: pkb\n"
            "  beta: employer\n",
            encoding="utf-8",
        )
        with unittest.mock.patch.dict(os.environ, {"APO_DESK_CONFIG": str(desk)}):
            out = ops.vault_op("merge")
        self.assertTrue(out["ok"])
        self.assertEqual(out["action"], "merge")
        self.assertFalse(out["full"])
        self.assertEqual(out["desk"]["citations"], "absolute_markdown")
        self.assertFalse(out["merge_rules"]["cross_pollinate_contracts"])
        self.assertEqual(out["vaults"]["alpha"]["role"], "pkb")
        self.assertEqual(out["vaults"]["beta"]["role"], "employer")
        self.assertIn("usage-contract", out["vaults"]["alpha"]["contract_ids"])
        self.assertNotIn("data", out["vaults"]["alpha"]["contracts"]["usage-contract"])
        self.assertEqual(out["desk_meta"]["source"], "file")
        self.assertIn("habits", out["desk"])
        self.assertIn("pointers", out["merge_rules"]["desk_overlay_keys"])
        full = ops.vault_op("merge", full=True)
        self.assertTrue(full["full"])
        self.assertEqual(
            full["vaults"]["alpha"]["contracts"]["usage-contract"]["data"]["purpose"],
            "test",
        )

    def test_merge_defaults_without_desk(self):
        missing = self.tmp / "no-such-desk.yaml"
        with unittest.mock.patch.dict(os.environ, {"APO_DESK_CONFIG": str(missing)}):
            # resolve_desk_path returns None when explicit path missing
            with unittest.mock.patch(
                "apo_engine.vault_desk.resolve_desk_path", return_value=None
            ):
                out = ops.vault_op("merge")
        self.assertTrue(out["ok"])
        self.assertEqual(out["desk_meta"]["source"], "defaults")
        self.assertNotIn("dual_write", out["desk"])
        self.assertNotIn("role", out["vaults"]["alpha"])


class PreferContractsDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-prefer-contracts-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_okf_and_git_prefer_contracts_dir(self):
        from apo_engine import git_contract, okf

        legacy = self.vault / "system" / "config"
        preferred = self.vault / "system" / "contracts"
        legacy.mkdir(parents=True)
        preferred.mkdir(parents=True)
        (legacy / "okf-contract.schema.yaml").write_text(
            "okf_version: '0.1'\nfrom: legacy\n", encoding="utf-8"
        )
        (preferred / "okf-contract.schema.yaml").write_text(
            "okf_version: '0.1'\nfrom: contracts\n", encoding="utf-8"
        )
        (legacy / "git-contract.schema.yaml").write_text(
            "git_contract_version: '0.1'\nremote: legacy\n", encoding="utf-8"
        )
        (preferred / "git-contract.schema.yaml").write_text(
            "git_contract_version: '0.1'\nremote: contracts\n", encoding="utf-8"
        )
        self.assertEqual(
            okf.resolve_contract_path(self.vault),
            preferred / "okf-contract.schema.yaml",
        )
        self.assertEqual(
            git_contract.resolve_git_contract_path(self.vault),
            preferred / "git-contract.schema.yaml",
        )


if __name__ == "__main__":
    unittest.main()
