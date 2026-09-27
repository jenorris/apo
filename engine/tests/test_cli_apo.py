"""``apo`` unified entry point — routing only (each backend's own commands
are exercised by ops/core tests; this checks the right backend gets called
with the right argv, and that help/version/unknown-command paths behave)."""

from __future__ import annotations

import unittest

from apo_engine import cli_apo


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self._orig_engine_main = cli_apo.engine_cli.main
        self._orig_ops_main = cli_apo.cli_ops.main
        self.engine_calls: list[list[str]] = []
        self.ops_calls: list[list[str]] = []

        def fake_engine_main(argv):
            self.engine_calls.append(list(argv))
            return 0

        def fake_ops_main(argv):
            self.ops_calls.append(list(argv))
            return 0

        cli_apo.engine_cli.main = fake_engine_main
        cli_apo.cli_ops.main = fake_ops_main

    def tearDown(self):
        cli_apo.engine_cli.main = self._orig_engine_main
        cli_apo.cli_ops.main = self._orig_ops_main

    def test_bare_note_verb_routes_to_cli_ops_with_full_argv(self):
        rc = cli_apo.main(["search", "kitchen lights", "--folder", "areas"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.ops_calls, [["search", "kitchen lights", "--folder", "areas"]])
        self.assertEqual(self.engine_calls, [])

    def test_note_verbs_cover_every_cli_ops_subcommand(self):
        # Every subcommand cli_ops.py's own parser defines must route here —
        # a literal table, not introspection, so this test is the guard that
        # keeps it in sync if cli_ops grows/renames a subcommand.
        for verb in (
            "read",
            "search",
            "write",
            "append",
            "patch",
            "patch-table",
            "graph-neighbors",
            "filter",
            "backlinks",
            "history",
        ):
            with self.subTest(verb=verb):
                self.ops_calls.clear()
                cli_apo.main([verb, "x"])
                self.assertEqual(self.ops_calls, [[verb, "x"]])

    def test_engine_group_strips_leading_token_and_forwards_rest(self):
        rc = cli_apo.main(["engine", "index", "--rebuild"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.engine_calls, [["index", "--rebuild"]])
        self.assertEqual(self.ops_calls, [])

    def test_engine_group_forwards_empty_rest(self):
        # No special-casing — apo-engine's own required-subparser error (or
        # --help) surfaces exactly as it would running apo-engine bare.
        cli_apo.main(["engine"])
        self.assertEqual(self.engine_calls, [[]])

    def test_unknown_command_errors_without_calling_either_backend(self):
        rc = cli_apo.main(["not-a-real-command"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.engine_calls, [])
        self.assertEqual(self.ops_calls, [])

    def test_help_short_circuits_before_any_backend(self):
        for args in (["--help"], ["-h"], ["help"], []):
            with self.subTest(args=args):
                self.engine_calls.clear()
                self.ops_calls.clear()
                rc = cli_apo.main(args)
                self.assertEqual(rc, 0)
                self.assertEqual(self.engine_calls, [])
                self.assertEqual(self.ops_calls, [])

    def test_version_short_circuits_before_any_backend(self):
        rc = cli_apo.main(["--version"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.engine_calls, [])
        self.assertEqual(self.ops_calls, [])

    def test_mcp_group_routes_to_mcp_server_main(self):
        from apo_engine.mcp import server as mcp_server

        orig = mcp_server.main
        calls = []
        mcp_server.main = lambda: calls.append(1)
        try:
            rc = cli_apo.main(["mcp"])
        finally:
            mcp_server.main = orig
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [1])
        self.assertEqual(self.engine_calls, [])
        self.assertEqual(self.ops_calls, [])


if __name__ == "__main__":
    unittest.main()
