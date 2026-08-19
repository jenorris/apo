"""Fenced mermaid extraction from markdown."""

from __future__ import annotations

import unittest

from apo_engine.mermaid_markdown import find_mermaid_fences


class MermaidMarkdownTest(unittest.TestCase):
    def test_single_fence(self):
        md = """# Title

```mermaid
flowchart LR
    A --> B
```

More prose.
"""
        fences = find_mermaid_fences(md.split("\n"))
        self.assertEqual(len(fences), 1)
        self.assertIn("A --> B", fences[0].text)

    def test_skips_code_fence(self):
        md = """```python
print('not mermaid')
```
"""
        self.assertEqual(find_mermaid_fences(md.split("\n")), [])


if __name__ == "__main__":
    unittest.main()
