"""Mermaid validation flaws."""

from __future__ import annotations

import unittest

from apo_engine.mermaid_validate import validate_mermaid_text


class MermaidValidateTest(unittest.TestCase):
    def test_invalid_syntax_flaw(self):
        flaws = validate_mermaid_text("flowchart TD\n  A -->", "bad.mmd")
        self.assertTrue(any(f.code == "mermaid.syntax" for f in flaws))

    def test_valid_no_error(self):
        flaws = validate_mermaid_text("flowchart LR\n  A --> B", "ok.mmd")
        errors = [f for f in flaws if f.severity == "error"]
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
