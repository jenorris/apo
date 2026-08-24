"""OKF dual-version read (v0.1 + v0.2) — no Ollama, no vault contract needed."""

from __future__ import annotations

import unittest
from datetime import date

from apo_engine import okf
from apo_engine.okf import v0_1, v0_2

V01_NOTE = """---
type: Reference
okf_type: Reference
title: Legacy note
description: written against v0.1
timestamp: "2026-01-02T03:04:05Z"
---

# Legacy note

Body text.

# Citations

* [Spec page](https://example.com/spec)
* [[internal-note]]
* bare-resource-string
"""

V02_NOTE = """---
type: Reference
okf_type: Reference
title: Modern note
description: written against v0.2
status: draft
stale_after: 2026-03-01
generated:
  by: reference_agent/gemini-2.5-pro
  at: "2026-02-01T00:00:00Z"
verified:
  - by: human:jeremy
    at: "2026-02-02T00:00:00Z"
usage_window:
  from: 2026-01-01
  to: 2026-01-31
sources:
  - id: spec
    resource: https://example.com/spec
    title: The spec
    author: human:ahormati
    usage_count: 12
    last_modified: 2026-01-15
  - resource: https://example.com/other
---

# Modern note

Body text.
"""


class DetectVersionTests(unittest.TestCase):
    def test_v0_1_note_detects_as_v0_1(self):
        meta = okf.read_concept(V01_NOTE, "resources/legacy.md")
        self.assertEqual(meta.detected_version, "0.1")

    def test_v0_2_note_detects_as_v0_2(self):
        meta = okf.read_concept(V02_NOTE, "resources/modern.md")
        self.assertEqual(meta.detected_version, "0.2")

    def test_bare_note_defaults_to_v0_1(self):
        meta = okf.read_concept("---\ntype: Note\n---\n\n# X\n", "resources/x.md")
        self.assertEqual(meta.detected_version, "0.1")

    def test_supported_versions(self):
        self.assertEqual(okf.SUPPORTED_VERSIONS, ("0.1", "0.2"))


class DualVersionReadTests(unittest.TestCase):
    """SPEC §13.1 supersessions, read from both sides."""

    def test_generated_at_falls_back_to_legacy_timestamp(self):
        meta = okf.read_concept(V01_NOTE, "resources/legacy.md")
        self.assertIsNone(meta.generated)
        self.assertEqual(meta.generated_at, "2026-01-02T03:04:05Z")

    def test_generated_at_prefers_generated_block(self):
        meta = okf.read_concept(V02_NOTE, "resources/modern.md")
        self.assertEqual(meta.generated_at, "2026-02-01T00:00:00Z")
        self.assertEqual(meta.generated.by.raw, "reference_agent/gemini-2.5-pro")

    def test_generated_wins_over_timestamp_when_both_present(self):
        note = (
            "---\ntype: Note\ntimestamp: \"2020-01-01T00:00:00Z\"\n"
            "generated:\n  by: process:nightly\n  at: \"2026-05-05T00:00:00Z\"\n---\n\n# X\n"
        )
        meta = okf.read_concept(note, "resources/x.md")
        self.assertEqual(meta.generated_at, "2026-05-05T00:00:00Z")
        self.assertEqual(meta.legacy_timestamp, "2020-01-01T00:00:00Z")

    def test_sources_fall_back_to_citations_body_list(self):
        meta = okf.read_concept(V01_NOTE, "resources/legacy.md")
        self.assertEqual(meta.sources, [])
        resources = [s.resource for s in meta.source_refs]
        self.assertEqual(
            resources,
            ["https://example.com/spec", "internal-note", "bare-resource-string"],
        )

    def test_sources_preferred_over_citations(self):
        note = V02_NOTE + "\n# Citations\n\n* [ignored](https://example.com/ignored)\n"
        meta = okf.read_concept(note, "resources/modern.md")
        resources = [s.resource for s in meta.source_refs]
        self.assertEqual(
            resources, ["https://example.com/spec", "https://example.com/other"]
        )

    def test_legacy_alt_date_fields_accepted(self):
        for key in ("updated", "ingested_at", "date"):
            note = f"---\ntype: Note\n{key}: \"2026-06-06T00:00:00Z\"\n---\n\n# X\n"
            meta = okf.read_concept(note, "resources/x.md")
            self.assertEqual(meta.generated_at, "2026-06-06T00:00:00Z", key)

    def test_strict_version_read_ignores_other_version(self):
        meta = okf.read_concept(V02_NOTE, "resources/modern.md", version="0.1")
        self.assertIsNone(meta.generated)
        self.assertEqual(meta.sources, [])

    def test_unknown_version_rejected(self):
        with self.assertRaises(ValueError):
            okf.read_concept(V02_NOTE, "resources/modern.md", version="9.9")


class ProvenanceTests(unittest.TestCase):
    def test_source_fields_parsed(self):
        meta = okf.read_concept(V02_NOTE, "resources/modern.md")
        first = meta.sources[0]
        self.assertEqual(first.id, "spec")
        self.assertEqual(first.resource, "https://example.com/spec")
        self.assertEqual(first.title, "The spec")
        self.assertEqual(first.author, "human:ahormati")
        self.assertEqual(first.usage_count, 12)
        self.assertEqual(first.last_modified, "2026-01-15")

    def test_shared_usage_window_frames_sources_without_one(self):
        """SPEC: the sibling usage_window frames every usage_count beneath it."""
        meta = okf.read_concept(V02_NOTE, "resources/modern.md")
        self.assertEqual(meta.usage_window.start, "2026-01-01")
        for ref in meta.sources:
            self.assertEqual(ref.usage_window.as_dict(), {"from": "2026-01-01", "to": "2026-01-31"})

    def test_per_source_window_overrides_shared(self):
        note = (
            "---\ntype: Note\nusage_window:\n  from: 2026-01-01\n  to: 2026-01-31\n"
            "sources:\n  - resource: https://example.com/a\n"
            "    usage_window:\n      from: 2025-01-01\n      to: 2025-12-31\n---\n\n# X\n"
        )
        meta = okf.read_concept(note, "resources/x.md")
        self.assertEqual(meta.sources[0].usage_window.start, "2025-01-01")

    def test_source_entry_without_resource_is_dropped(self):
        note = "---\ntype: Note\nsources:\n  - title: no resource\n---\n\n# X\n"
        meta = okf.read_concept(note, "resources/x.md")
        self.assertEqual(meta.sources, [])

    def test_bare_string_source_accepted(self):
        note = "---\ntype: Note\nsources:\n  - https://example.com/a\n---\n\n# X\n"
        meta = okf.read_concept(note, "resources/x.md")
        self.assertEqual(meta.sources[0].resource, "https://example.com/a")


class TrustTests(unittest.TestCase):
    def test_bare_verified_mapping_reads_as_one_element_list(self):
        """SPEC §11 consumer obligation."""
        note = (
            "---\ntype: Note\nverified:\n  by: human:jeremy\n"
            "  at: \"2026-02-02T00:00:00Z\"\n---\n\n# X\n"
        )
        meta = okf.read_concept(note, "resources/x.md")
        self.assertEqual(len(meta.verified), 1)
        self.assertEqual(meta.verified[0].by.raw, "human:jeremy")

    def test_human_verification_detected(self):
        meta = okf.read_concept(V02_NOTE, "resources/modern.md")
        self.assertTrue(meta.is_human_verified)

    def test_agent_verification_is_not_human(self):
        note = (
            "---\ntype: Note\nverified:\n  - by: reference_agent/gemini-2.5-pro\n"
            "    at: \"2026-02-02T00:00:00Z\"\n---\n\n# X\n"
        )
        meta = okf.read_concept(note, "resources/x.md")
        self.assertFalse(meta.is_human_verified)

    def test_actor_convention(self):
        """SPEC §7 — three identity forms."""
        human = okf.parse_actor("human:jeremy")
        self.assertTrue(human.is_human)
        self.assertEqual(human.ident, "jeremy")

        proc = okf.parse_actor("process:finance-nightly")
        self.assertEqual(proc.kind, "process")
        self.assertEqual(proc.ident, "finance-nightly")

        agent = okf.parse_actor("reference_agent/gemini-2.5-pro")
        self.assertEqual(agent.kind, "agent")
        self.assertFalse(agent.is_human)

        self.assertIsNone(okf.parse_actor(""))


class LifecycleTests(unittest.TestCase):
    def test_status_default_is_stable(self):
        meta = okf.read_concept("---\ntype: Note\n---\n\n# X\n", "resources/x.md")
        self.assertIsNone(meta.status)
        self.assertEqual(v0_2.effective_status(meta), "stable")

    def test_explicit_status_kept(self):
        meta = okf.read_concept(V02_NOTE, "resources/modern.md")
        self.assertEqual(v0_2.effective_status(meta), "draft")

    def test_unknown_status_is_surfaced_not_rejected(self):
        note = "---\ntype: Note\nstatus: bespoke\n---\n\n# X\n"
        meta = okf.read_concept(note, "resources/x.md")
        self.assertEqual(v0_2.effective_status(meta), "bespoke")

    def test_stale_after_in_past_is_stale(self):
        meta = okf.read_concept(V02_NOTE, "resources/modern.md")
        self.assertTrue(meta.is_stale(date(2026, 3, 2)))
        self.assertFalse(meta.is_stale(date(2026, 2, 28)))

    def test_missing_stale_after_is_never_stale(self):
        meta = okf.read_concept("---\ntype: Note\n---\n\n# X\n", "resources/x.md")
        self.assertFalse(meta.is_stale(date(2099, 1, 1)))

    def test_unparseable_stale_after_is_not_stale(self):
        note = "---\ntype: Note\nstale_after: soon\n---\n\n# X\n"
        meta = okf.read_concept(note, "resources/x.md")
        self.assertFalse(meta.is_stale(date(2099, 1, 1)))


class RobustnessTests(unittest.TestCase):
    def test_malformed_yaml_degrades_to_scalar_view(self):
        """A hand-broken block must still yield core fields, not vanish."""
        note = "---\ntype: Note\ntitle: X\n\tbroken: [unclosed\n---\n\n# X\n"
        meta = okf.read_concept(note, "resources/x.md")
        self.assertEqual(meta.type, "Note")
        self.assertEqual(meta.title, "X")

    def test_no_frontmatter_yields_empty_meta(self):
        meta = okf.read_concept("# Just a body\n", "resources/x.md")
        self.assertIsNone(meta.type)
        self.assertEqual(meta.sources, [])

    def test_tags_list_and_csv(self):
        listed = okf.read_concept(
            "---\ntype: Note\ntags:\n  - a\n  - b\n---\n\n# X\n", "resources/x.md"
        )
        self.assertEqual(listed.tags, ["a", "b"])
        csv = okf.read_concept("---\ntype: Note\ntags: a, b\n---\n\n# X\n", "resources/x.md")
        self.assertEqual(csv.tags, ["a", "b"])

    def test_citations_stops_at_next_heading(self):
        body = "# Citations\n\n* [a](https://a.example)\n\n# Notes\n\n* [b](https://b.example)\n"
        self.assertEqual(v0_1.parse_citations(body), ["https://a.example"])

    def test_as_dict_round_trip(self):
        meta = okf.read_concept(V02_NOTE, "resources/modern.md")
        out = meta.as_dict()
        self.assertEqual(out["okf_version"], "0.2")
        self.assertEqual(out["generated"], {"by": "reference_agent/gemini-2.5-pro", "at": "2026-02-01T00:00:00Z"})
        self.assertEqual(out["sources"][0]["usage_count"], 12)
        self.assertEqual(out["generated_at"], "2026-02-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
