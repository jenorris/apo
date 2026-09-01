#!/usr/bin/env python3
"""Paginated vault lint with mechanical auto-fix (trailing whitespace).

Usage:
  APO_VAULT_PATHS=~/Notes/Work APO_DEFAULT_VAULT=work \\
    python engine/scripts/lint_batch.py --vault work [--limit 500]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from apo_engine import ops  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Paginated vault lint + mechanical fix")
    p.add_argument("--vault", default="", help="Vault id (default registry default)")
    p.add_argument("--folder", default="", help="Optional folder scope")
    p.add_argument("--limit", type=int, default=500, help="Flaws per page")
    args = p.parse_args()

    offset = 0
    total_fixed = 0
    total_seen = 0
    grand_total: int | None = None
    while True:
        out = ops.vault_op(
            "lint",
            vault=args.vault or None,
            folder=args.folder or None,
            fix=True,
            limit=args.limit,
            offset=offset,
        )
        if not out.get("ok"):
            print(f"error: {out.get('error')} {out.get('message')}", file=sys.stderr)
            return 1
        if grand_total is None:
            grand_total = int(out.get("total_flaws") or 0)
        batch = out.get("flaws") or []
        fixed = sum(1 for f in batch if f.get("fixed"))
        total_fixed += fixed
        total_seen += len(batch)
        print(
            f"offset={offset} page={len(batch)} fixed={fixed} "
            f"total_flaws={grand_total} scanned={total_seen}"
        )
        if not batch or len(batch) < args.limit:
            break
        offset += args.limit

    print(f"done: scanned={total_seen} mechanical_fixed={total_fixed} vault_total={grand_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
