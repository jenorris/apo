"""Agent-facing validation hint rewrites + patch error flattening."""

from __future__ import annotations

import unittest

from pydantic import ValidationError as PydanticValidationError
from pydantic import TypeAdapter

from apo_engine.patch_ops import PatchOp
from apo_engine.validation_hints import (
    flatten_patch_failure_error,
    format_tool_validation_error,
)


def _patch_ops_validation_error(ops: list[dict]) -> Exception:
    """Build a FastMCP-like ValidationError wrapping a pydantic call error."""
    try:
        TypeAdapter(list[PatchOp]).validate_python(ops)
    except PydanticValidationError as e:
        # Mimic fastmcp.exceptions.ValidationError(str(e)) with cause
        from fastmcp.exceptions import ValidationError as FastMCPValidationError

        wrapped = FastMCPValidationError(str(e))
        wrapped.__cause__ = e
        return wrapped
    raise AssertionError("expected validation failure")


class FormatToolValidationErrorTest(unittest.TestCase):
    def test_patch_note_missing_op_discriminator(self):
        exc = _patch_ops_validation_error(
            [{"field": "content", "old": "a", "value": "b"}]
        )
        msg = format_tool_validation_error("patch_note", exc)
        self.assertIn('missing required "op"', msg)
        self.assertIn("replace_text", msg)
        self.assertIn("field/find/replace", msg)
        self.assertNotIn("union_tag_not_found", msg)
        self.assertNotIn("errors.pydantic.dev", msg)

    def test_read_note_snippet_chars(self):
        class Fake(Exception):
            def errors(self, include_url: bool = True):
                return [
                    {
                        "type": "unexpected_keyword_argument",
                        "loc": ("snippet_chars",),
                        "msg": "Unexpected keyword argument",
                        "input": 0,
                    }
                ]

        exc = Exception("1 validation error")
        exc.__cause__ = Fake()
        msg = format_tool_validation_error("read_note", exc)
        self.assertIn("snippet_chars", msg)
        self.assertIn("max_chars", msg)
        self.assertIn("search_notes", msg)

    def test_expand_chunk_path_heading(self):
        class Fake(Exception):
            def errors(self, include_url: bool = True):
                return [
                    {
                        "type": "missing_argument",
                        "loc": ("chunk_hash",),
                        "msg": "Missing required argument",
                        "input": {"path": "a.md", "heading": "History"},
                    },
                    {
                        "type": "unexpected_keyword_argument",
                        "loc": ("path",),
                        "msg": "Unexpected keyword argument",
                        "input": "a.md",
                    },
                    {
                        "type": "unexpected_keyword_argument",
                        "loc": ("heading",),
                        "msg": "Unexpected keyword argument",
                        "input": "History",
                    },
                ]

        exc = Exception("boom")
        exc.__cause__ = Fake()
        msg = format_tool_validation_error("expand_chunk", exc)
        self.assertIn("chunk_hash", msg)
        self.assertIn("read_note", msg)
        self.assertNotIn("Unexpected keyword argument", msg)

    def test_fallback_strips_pydantic_url(self):
        class Fake(Exception):
            def errors(self, include_url: bool = True):
                return []

        exc = Exception(
            "1 validation error for call[foo]\n"
            "bar\n"
            "  something weird [type=whatever]\n"
            "    For further information visit https://errors.pydantic.dev/2.13/v/whatever"
        )
        exc.__cause__ = Fake()
        msg = format_tool_validation_error("foo", exc)
        self.assertIn("Invalid arguments for foo", msg)
        self.assertNotIn("errors.pydantic.dev", msg)


class FlattenPatchFailureErrorTest(unittest.TestCase):
    def test_nested_dict(self):
        out = flatten_patch_failure_error(
            {"op_index": 0, "code": "anchor_not_found", "message": "heading 'X' not found"},
            suggestions=[{"heading": "History"}],
        )
        self.assertEqual(out["error"], "anchor_not_found")
        self.assertIn("heading 'X'", out["message"])
        self.assertEqual(out["op_index"], 0)
        self.assertEqual(out["error_detail"]["code"], "anchor_not_found")
        self.assertEqual(out["suggestions"], [{"heading": "History"}])

    def test_none(self):
        out = flatten_patch_failure_error(None)
        self.assertEqual(out["error"], "patch_failed")


if __name__ == "__main__":
    unittest.main()
