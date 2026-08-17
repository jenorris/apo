"""Comment-preserving YAML round-trip (``yaml_rt``) across all three write sites.

Covers the Phase 1 contract: an edit to one key must not reflow, requote, or
strip comments from the rest of the document — for standalone ``.yaml`` catalog
notes, Markdown frontmatter fences, and YAML patch ops alike.
"""

from __future__ import annotations

import datetime
import unittest

import yaml

from apo_engine import yaml_rt
from apo_engine.markdown_patch import PatchError, apply_patch
from apo_engine.note_format import dump_yaml_document, parse_yaml_document
from apo_engine.yaml_patch import apply_yaml_patch, set_yaml_fields

# Every comment shape in one document: leading block, inline, mid-document block,
# blank-line separation, flow style, nested map, indented sequence.
COMMENTED_YAML = """# catalog record — hand maintained
title: Alpha Queue     # display name
status: open
# the meta block is read by the sweeper
meta:
  owner: jeremy        # current owner
  tags: [ops, queue]   # flow style on purpose
  members:
    - alice
    - bob

# trailing note
count: 3
"""

COMMENTED_MD = """---
title: Test Thread     # display name
# status is set by the watcher
status: open
tags:
  - alpha
  - beta
---

# Body

Prose that must not move.
"""


def comment_column(text: str, key_prefix: str) -> int:
    """Column of the trailing ``#`` on the line starting with ``key_prefix``."""
    for line in text.split("\n"):
        if line.lstrip().startswith(key_prefix):
            return line.index("#")
    raise AssertionError(f"{key_prefix!r} not in {text!r}")


def fence_body(content: str) -> str:
    """Frontmatter text between the leading ``---`` fences."""
    lines = content.split("\n")
    assert lines[0].strip() == "---", content
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    return "\n".join(lines[1:end])


class TestLoadDump(unittest.TestCase):
    def test_identity_round_trip(self):
        data = yaml_rt.load(COMMENTED_YAML)
        self.assertIsInstance(data, dict)
        self.assertEqual(yaml_rt.dump(data), COMMENTED_YAML)

    def test_non_mapping_and_unparseable_return_none(self):
        self.assertIsNone(yaml_rt.load("- a\n- b\n"))
        self.assertIsNone(yaml_rt.load("title: 'unterminated\n"))
        self.assertIsNone(yaml_rt.load(""))
        self.assertIsNone(yaml_rt.load("   \n"))
        self.assertIsNone(yaml_rt.load(None))

    def test_unknown_tag_falls_back_to_pyyaml_path(self):
        # ruamel would keep this as a TaggedScalar the catalog cannot carry.
        self.assertIsNone(yaml_rt.load("a: !custom value\n"))

    def test_dump_empty_and_none(self):
        self.assertEqual(yaml_rt.dump(None), "{}\n")
        self.assertEqual(yaml_rt.dump({}), "{}\n")

    def test_plain_dict_keeps_legacy_layout(self):
        # Freshly built mappings must emit exactly as safe_dump did.
        legacy = yaml.safe_dump(
            {"title": "T", "n": 1, "items": ["a", "b"], "meta": {"owner": "x"}},
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            width=1000,
        )
        self.assertEqual(
            yaml_rt.dump({"title": "T", "n": 1, "items": ["a", "b"], "meta": {"owner": "x"}}),
            legacy,
        )

    def test_null_spelled_like_pyyaml(self):
        self.assertEqual(yaml_rt.dump({"due_date": None}), "due_date: null\n")

    def test_yaml_1_1_bools_stay_strings_for_pyyaml_readers(self):
        # ruamel emits YAML 1.2; unquoted `yes` would read back as True in PyYAML.
        for value in ("yes", "no", "on", "off"):
            out = yaml_rt.dump({"flag": value})
            self.assertEqual(yaml.safe_load(out)["flag"], value, out)

    def test_tolerant_timestamp_regression(self):
        # Invalid YAML 1.1 date — must load as a string, not raise (t_1e8f0ff4).
        data = yaml_rt.load("effective_date: 2017-00-00\ntitle: X\n")
        self.assertIsNotNone(data)
        self.assertEqual(data["effective_date"], "2017-00-00")
        self.assertIsInstance(data["effective_date"], str)
        self.assertEqual(yaml.safe_load(yaml_rt.dump(data))["effective_date"], "2017-00-00")

    def test_valid_date_still_a_date(self):
        data = yaml_rt.load("day: 2026-08-17\n")
        self.assertIsInstance(data["day"], datetime.date)

    def test_set_and_delete_at_path_helpers(self):
        data = yaml_rt.load(COMMENTED_YAML)
        yaml_rt.set_field_at_path(data, "meta.owner", "lyra")
        self.assertEqual(data["meta"]["owner"], "lyra")
        yaml_rt.delete_field_at_path(data, "status")
        self.assertNotIn("status", data)
        self.assertIn("# current owner", yaml_rt.dump(data))


class TestYamlNoteComments(unittest.TestCase):
    """Standalone ``.yaml`` catalog notes (note_format + yaml_patch)."""

    def test_set_unrelated_key_leaves_every_other_line_untouched(self):
        result = apply_yaml_patch(
            COMMENTED_YAML, [{"op": "set_field", "field": "count", "value": 4}]
        )
        self.assertTrue(result.ok, result.results)
        before = COMMENTED_YAML.split("\n")
        after = result.content.split("\n")
        self.assertEqual(len(before), len(after))
        for i, (a, b) in enumerate(zip(before, after)):
            if a.startswith("count:"):
                self.assertEqual(b, "count: 4")
            else:
                self.assertEqual(a, b, f"line {i} changed")

    def test_set_edited_key_keeps_its_inline_comment(self):
        result = apply_yaml_patch(
            COMMENTED_YAML, [{"op": "set_field", "field": "title", "value": "Beta Queue"}]
        )
        self.assertTrue(result.ok, result.results)
        # The comment is re-emitted at its original column, not glued to the value.
        self.assertIn("title: Beta Queue      # display name", result.content)
        self.assertEqual(comment_column(result.content, "title:"), comment_column(COMMENTED_YAML, "title:"))

    def test_set_nested_key_preserves_flow_style_and_indent(self):
        result = apply_yaml_patch(
            COMMENTED_YAML, [{"op": "set_field", "field": "meta.owner", "value": "lyra"}]
        )
        self.assertTrue(result.ok, result.results)
        self.assertIn("owner: lyra          # current owner", result.content)
        self.assertIn("tags: [ops, queue]   # flow style on purpose", result.content)
        self.assertIn("    - alice", result.content)

    def test_delete_drops_the_key_and_the_comments_it_owns(self):
        result = apply_yaml_patch(
            COMMENTED_YAML, [{"op": "delete_field", "field": "meta"}]
        )
        self.assertTrue(result.ok, result.results)
        self.assertNotIn("owner: jeremy", result.content)
        self.assertNotIn("# current owner", result.content)
        # Documented: a comment block *following* the deleted key belongs to it in
        # ruamel's model, so it goes too.
        self.assertNotIn("# trailing note", result.content)
        # Everything above the deleted key is untouched.
        self.assertTrue(
            result.content.startswith(
                "# catalog record — hand maintained\n"
                "title: Alpha Queue     # display name\n"
                "status: open\n"
                "# the meta block is read by the sweeper\n"
            ),
            result.content,
        )
        self.assertIn("count: 3", result.content)

    def test_new_key_appends_without_reformatting(self):
        result = apply_yaml_patch(
            COMMENTED_YAML, [{"op": "set_field", "field": "priority", "value": 90}]
        )
        self.assertTrue(result.ok, result.results)
        self.assertTrue(result.content.startswith(COMMENTED_YAML.rstrip("\n") + "\n"))
        self.assertIn("priority: 90", result.content)

    def test_okf_stamp_preserves_comments(self):
        out = set_yaml_fields(COMMENTED_YAML, {"okf_type": "Fact", "description": "queue"})
        self.assertIn("# catalog record — hand maintained", out)
        self.assertIn("tags: [ops, queue]   # flow style on purpose", out)
        self.assertIn("okf_type: Fact", out)

    def test_parse_dump_round_trip_through_note_format(self):
        data = parse_yaml_document(COMMENTED_YAML)
        self.assertIsInstance(data, dict)
        self.assertEqual(data["meta"]["owner"], "jeremy")
        self.assertEqual(dump_yaml_document(data), COMMENTED_YAML)

    def test_parse_falls_back_for_documents_ruamel_refuses(self):
        # Duplicate keys: ruamel raises, PyYAML takes the last one.
        data = parse_yaml_document("title: a\ntitle: b\n")
        self.assertEqual(data, {"title": "b"})
        self.assertIsNone(parse_yaml_document("- not\n- a mapping\n"))

    def test_stub_and_empty_document(self):
        self.assertEqual(dump_yaml_document({"title": "Foo"}), "title: Foo\n")
        self.assertEqual(dump_yaml_document({}), "{}\n")


class TestFrontmatterComments(unittest.TestCase):
    """Markdown frontmatter fences (markdown_patch)."""

    def test_set_unrelated_field_is_byte_identical_elsewhere(self):
        result = apply_patch(
            COMMENTED_MD, [{"op": "set_field", "field": "status", "value": "resolved"}]
        )
        self.assertTrue(result.ok, result.results)
        before = COMMENTED_MD.split("\n")
        after = result.content.split("\n")
        self.assertEqual(len(before), len(after))
        for a, b in zip(before, after):
            if a.startswith("status:"):
                self.assertEqual(b, "status: resolved")
            else:
                self.assertEqual(a, b)

    def test_set_field_keeps_inline_comment_on_edited_key(self):
        result = apply_patch(
            COMMENTED_MD, [{"op": "set_field", "field": "title", "value": "Renamed"}]
        )
        self.assertTrue(result.ok, result.results)
        self.assertIn("title: Renamed         # display name", result.content)
        self.assertEqual(
            comment_column(result.content, "title:"), comment_column(COMMENTED_MD, "title:")
        )

    def test_new_field_leaves_existing_lines_and_body_alone(self):
        result = apply_patch(
            COMMENTED_MD, [{"op": "set_field", "field": "priority", "value": 90}]
        )
        self.assertTrue(result.ok, result.results)
        body = fence_body(result.content)
        self.assertTrue(body.startswith(fence_body(COMMENTED_MD)))
        self.assertIn("priority: 90", body)
        self.assertIn("Prose that must not move.", result.content)

    def test_delete_field_keeps_other_comments_and_body(self):
        result = apply_patch(COMMENTED_MD, [{"op": "delete_field", "field": "tags"}])
        self.assertTrue(result.ok, result.results)
        self.assertNotIn("- alpha", result.content)
        self.assertIn("title: Test Thread     # display name", result.content)
        self.assertIn("# status is set by the watcher", result.content)
        self.assertIn("Prose that must not move.", result.content)

    def test_sequence_indent_survives_an_unrelated_edit(self):
        result = apply_patch(
            COMMENTED_MD, [{"op": "set_field", "field": "status", "value": "resolved"}]
        )
        self.assertIn("tags:\n  - alpha\n  - beta\n", result.content)

    def test_flush_sequence_indent_also_survives(self):
        src = "---\ntitle: X\ntags:\n- a\n- b\n---\n\nbody\n"
        result = apply_patch(src, [{"op": "set_field", "field": "title", "value": "Y"}])
        self.assertTrue(result.ok, result.results)
        self.assertIn("tags:\n- a\n- b\n", result.content)

    def test_invalid_timestamp_stays_a_quoted_string(self):
        result = apply_patch(
            COMMENTED_MD, [{"op": "set_field", "field": "effective_date", "value": "2017-00-00"}]
        )
        self.assertTrue(result.ok, result.results)
        self.assertRegex(result.content, r"effective_date:\s*['\"]2017-00-00['\"]")
        self.assertEqual(
            yaml.safe_load(fence_body(result.content))["effective_date"], "2017-00-00"
        )

    def test_empty_fence(self):
        result = apply_patch("---\n---\n\n# B\n", [{"op": "set_field", "field": "a", "value": "b"}])
        self.assertTrue(result.ok, result.results)
        self.assertEqual(result.content, "---\na: b\n---\n\n# B\n")

    def test_no_frontmatter_creates_a_fence(self):
        result = apply_patch("# Just body\n", [{"op": "set_field", "field": "title", "value": "X"}])
        self.assertTrue(result.ok, result.results)
        self.assertEqual(result.content, "---\ntitle: X\n---\n\n# Just body\n")

    def test_missing_closing_fence_is_treated_as_no_frontmatter(self):
        # Unchanged pre-existing behavior: no bounds → a fresh fence is prepended.
        result = apply_patch("---\ntitle: X\n\n# B\n", [{"op": "set_field", "field": "a", "value": "b"}])
        self.assertTrue(result.ok, result.results)
        self.assertTrue(result.content.startswith("---\na: b\n---\n"))
        self.assertIn("title: X", result.content)

    def test_delete_without_frontmatter_still_errors(self):
        result = apply_patch("# Body\n", [{"op": "delete_field", "field": "x"}], strict=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "invalid_frontmatter")

    def test_broken_frontmatter_still_reports_invalid(self):
        result = apply_patch(
            "---\ntitle: 'x\n---\n\nbody\n",
            [{"op": "set_field", "field": "a", "value": "b"}],
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "invalid_frontmatter")


# Real frontmatter blocks lifted from the atlas and lyra vaults (2026-08-17).
CORPUS_IDENTICAL = [
    # atlas:system/config/source-routing.md — flush sequences, nested list of maps,
    # mixed quote styles.
    """title: External source routing
type: note
permalink: system-config-source-routing
status: active
tags:
- meta
- agent
- automation
relations:
- type: about
  ref: '[[AGENT]]'
- type: derived-from
  ref: '[[system/config/github-pr-triage]]'
okf_type: Note
description: External source routing
timestamp: "2026-08-05T00:05:04Z"
""",
    # lyra:areas/memory/project/…-reddit-mcp-status.md — double-quoted scalars.
    """title: "Reddit MCP status — wired but network-blocked"
type: note
memory_type: project
okf_type: Fact
timestamp: "2026-08-13T04:54:28Z"
status: active
""",
    # atlas:README.md — indented sequences of wiki-links.
    """title: README
type: note
permalink: 20260514-f9ee67f4
related:
  - "[[system/templates/frontmatter-schema]]"
  - "[[inbox/README]]"
  - "[[AGENT]]"
tags:
  - PARA
  - meta
""",
]

# Known non-identity: ruamel drops the padding inside a flow mapping. The value is
# unchanged, so this is cosmetic — recorded here so a future backend swap notices.
CORPUS_VALUE_ONLY = [
    """title: README
relations:
  - { type: about, ref: "[[system/templates/frontmatter-schema]]" }
""",
    # A long quoted scalar the original writer wrapped: width=1000 unwraps it (the
    # same thing safe_dump did before this module existed).
    """description: 'Target architecture for Lyra memory: provider shims own memory access;
  MCP stays for vault notes/contracts; hot core stays native.'
status: active
""",
]


class TestVaultCorpus(unittest.TestCase):
    def test_identity_round_trip(self):
        for src in CORPUS_IDENTICAL:
            with self.subTest(src=src.split("\n", 1)[0]):
                data = yaml_rt.load(src)
                self.assertIsNotNone(data)
                self.assertEqual(yaml_rt.dump(data), src)

    def test_value_preserved_where_layout_is_not(self):
        for src in CORPUS_VALUE_ONLY:
            with self.subTest(src=src.split("\n", 1)[0]):
                data = yaml_rt.load(src)
                self.assertIsNotNone(data)
                self.assertEqual(yaml.safe_load(yaml_rt.dump(data)), yaml.safe_load(src))

    def test_unrelated_edit_only_touches_one_line(self):
        for src in CORPUS_IDENTICAL:
            if "\nstatus:" not in f"\n{src}":
                continue  # nothing to overwrite in place
            with self.subTest(src=src.split("\n", 1)[0]):
                md = f"---\n{src}---\n\n# Body\n"
                result = apply_patch(
                    md, [{"op": "set_field", "field": "status", "value": "archived"}]
                )
                self.assertTrue(result.ok, result.results)
                changed = [
                    (a, b)
                    for a, b in zip(md.split("\n"), result.content.split("\n"))
                    if a != b
                ]
                self.assertLessEqual(len(changed), 1, changed)


if __name__ == "__main__":
    unittest.main()
