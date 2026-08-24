#!/usr/bin/env python3
"""
okf_export.py — shim over ``apo-engine okf export``.

The real implementation lives in ``apo_engine.okf_cli``. This file used to be a
second, independent export path (its own frontmatter parser, its own type map)
that could drift from the engine; it is kept only so existing
``just okf-export`` invocations and scripts keep working.

  python3 okf_export.py --vault ~/Notes/Meta /tmp/export
  python3 okf_export.py --vault ~/Notes/Meta /tmp/export.tar.gz --archive
"""

from __future__ import annotations

import sys

from _bootstrap import bootstrap, split_vault_args
from okf_shim import run_engine_okf


def main(argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    root, raw = bootstrap(raw)
    _, raw = split_vault_args(raw)
    return run_engine_okf("export", root, raw)


if __name__ == "__main__":
    raise SystemExit(main())
