"""Phase 3 — forward-only `generated` emission + documented index visibility bound."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from apo_engine import okf, ops

BASE_CONTRACT = """
okf_version: "0.2"
type_field: okf_type
legacy_type_field: type
spec_type_field: type
spec_type_policy: fill
core_required:
  - okf_type
  - description
  - timestamp
default_enforcement: soft
default_okf_type: Note
reserved_filenames:
  - index.md
  - log.md
path_rules:
  - match: "areas/threads/**/*.md"
    enforcement: soft
    okf_type: Thread
"""

FORWARD = BASE_CONTRACT + '\ngenerated_policy: forward\ngenerated_by: "apo/engine-test"\n'


class GeneratedEmissionTests(unittest.TestCase):
    def setUp(self):
        okf.clear_contract_cache()
        self._env = {}
        for key in ("APO_OKF_CONTRACT", "APO_OKF_ENFORCEMENT", "APO_OKF_GENERATED"):
            self._env[key] = os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.contract = self.root / "system" / "contracts" / "okf-contract.schema.yaml"
        self.contract.parent.mkdir(parents=True)
        self.set_contract(BASE_CONTRACT)

    def tearDown(self):
        okf.clear_contract_cache()
        self.tmp.cleanup()
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def set_contract(self, text: str) -> None:
        self.contract.write_text(text, encoding="utf-8")
        okf.clear_contract_cache()

    def stamp(self, content: str, rel: str = "areas/threads/a.md", **kw):
        return okf.process_concept(
            vault_root=self.root, rel_path=rel, content=content, **kw
        )

    def test_default_policy_emits_nothing(self):
        """v0.2 provenance is opt-in; the default vault behavior is unchanged."""
        r = self.stamp("# A\n\nbody\n")
        self.assertNotIn("generated", r.stamped)
        self.assertNotIn("generated:", r.content)

    def test_forward_policy_stamps_generated_on_new_note(self):
        self.set_contract(FORWARD)
        r = self.stamp("# A\n\nbody\n")
        self.assertIn("generated", r.stamped)
        self.assertRegex(r.content, r'(?m)^generated: \{ by: "apo/engine-test", at: "')

    def test_generated_is_readable_as_nested_mapping(self):
        """Flow style must round-trip through the v0.2 structured reader."""
        self.set_contract(FORWARD)
        r = self.stamp("# A\n\nbody\n")
        meta = okf.read_concept(r.content, "areas/threads/a.md")
        self.assertEqual(meta.detected_version, "0.2")
        self.assertIsNotNone(meta.generated)
        self.assertEqual(meta.generated.by.raw, "apo/engine-test")
        self.assertTrue(meta.generated.at)

    def test_generated_at_agrees_with_timestamp(self):
        self.set_contract(FORWARD)
        r = self.stamp("# A\n\nbody\n")
        meta = okf.read_concept(r.content, "areas/threads/a.md")
        # Both were stamped by the same write, so the v0.2 reader and the v0.1
        # fallback must not disagree about when this changed.
        self.assertEqual(meta.generated.at, meta.legacy_timestamp)

    def test_existing_generated_not_backfilled_or_touched(self):
        self.set_contract(FORWARD)
        content = (
            '---\nokf_type: Thread\ntype: Thread\ndescription: d\n'
            'timestamp: "2026-01-01T00:00:00Z"\ntitle: t\n'
            'generated: { by: "human:jeremy", at: "2020-01-01T00:00:00Z" }\n---\n\n# A\n'
        )
        r = self.stamp(content)
        self.assertNotIn("generated", r.stamped)
        self.assertIn('by: "human:jeremy"', r.content)
        self.assertIn('at: "2020-01-01T00:00:00Z"', r.content)

    def test_generated_refreshes_when_timestamp_bumps(self):
        self.set_contract(FORWARD)
        content = (
            '---\nokf_type: Thread\ntype: Thread\ndescription: d\n'
            'timestamp: "2020-01-01T00:00:00Z"\ntitle: t\n'
            'generated: { by: "apo/old", at: "2020-01-01T00:00:00Z" }\n---\n\n# A\n'
        )
        r = self.stamp(content, bump_timestamp=True)
        self.assertIn("generated", r.stamped)
        self.assertIn('by: "apo/engine-test"', r.content)
        self.assertNotIn('at: "2020-01-01T00:00:00Z"', r.content)

    def test_env_can_force_forward(self):
        os.environ["APO_OKF_GENERATED"] = "forward"
        r = self.stamp("# A\n\nbody\n")
        self.assertIn("generated", r.stamped)

    def test_env_can_force_off(self):
        self.set_contract(FORWARD)
        os.environ["APO_OKF_GENERATED"] = "off"
        r = self.stamp("# A\n\nbody\n")
        self.assertNotIn("generated", r.stamped)

    def test_actor_with_quotes_is_escaped(self):
        self.set_contract(BASE_CONTRACT + '\ngenerated_policy: forward\ngenerated_by: \'a"b\'\n')
        r = self.stamp("# A\n\nbody\n")
        meta = okf.read_concept(r.content, "areas/threads/a.md")
        self.assertEqual(meta.generated.by.raw, 'a"b')

    def test_stamped_note_still_passes_both_profiles(self):
        self.set_contract(FORWARD)
        r = self.stamp("# A\n\nbody\n")
        for profile in ("okf", "apo"):
            report = okf.validate_concept(
                vault_root=self.root,
                rel_path="areas/threads/a.md",
                content=r.content,
                profile=profile,
            )
            self.assertTrue(report.ok, (profile, report.violations))

    def test_invalid_policy_falls_back_to_off(self):
        self.set_contract(BASE_CONTRACT + "\ngenerated_policy: bogus\n")
        contract = okf.get_contract(self.root)
        self.assertEqual(contract.generated_policy, "off")


class IndexVisibilityTests(unittest.TestCase):
    """The write/read race now has a written-down bound instead of silence."""

    def test_watcher_down_is_unbounded(self):
        out = ops.index_visibility(running=False)
        self.assertFalse(out["watcher_running"])
        self.assertIsNone(out["bound_seconds"])
        self.assertEqual(out["path"], "blocked")

    def test_woken_write_is_bounded_by_debounce(self):
        from apo_engine import config

        out = ops.index_visibility(woken=True, running=True)
        self.assertEqual(out["path"], "wake")
        self.assertEqual(out["bound_seconds"], round(float(config.WATCH_DEBOUNCE), 1))

    def test_unwoken_write_adds_the_poll_interval(self):
        from apo_engine import config

        out = ops.index_visibility(woken=False, running=True)
        self.assertEqual(out["path"], "poll")
        self.assertEqual(
            out["bound_seconds"],
            round(float(config.WATCH_DEBOUNCE) + float(config.WATCH_POLL_INTERVAL), 1),
        )

    def test_bound_is_scheduling_only_and_says_so(self):
        out = ops.index_visibility(woken=True, running=True)
        self.assertIn("embed", out["note"])


if __name__ == "__main__":
    unittest.main()
