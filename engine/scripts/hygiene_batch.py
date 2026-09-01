#!/usr/bin/env python3
"""Batch corpus hygiene (frontmatter floor, cross-vault wikilinks, dialect stubs).

Usage:
  APO_VAULT_PATHS=~/Notes/Work:~/Notes/Atlas APO_DEFAULT_VAULT=work \\
    python engine/scripts/hygiene_batch.py --vault work --folder areas [--dry-run]

  APO_VAULT_PATHS=~/Notes/Work:~/Notes/Atlas APO_DEFAULT_VAULT=work \\
    python engine/scripts/hygiene_batch.py --vault work --folder projects
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from apo_engine import hygiene_apply, vaults  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Batch vault hygiene (FM, links, dialect)")
    p.add_argument("--vault", default="", help="Vault id (default registry default)")
    p.add_argument("--folder", default="", help="Folder scope (e.g. areas, projects)")
    p.add_argument("--dry-run", action="store_true", help="Report only; do not write")
    p.add_argument("--limit", type=int, default=None, help="Max notes to scan")
    p.add_argument("--no-frontmatter", action="store_true")
    p.add_argument("--no-links", action="store_true")
    p.add_argument("--no-dialect", action="store_true")
    p.add_argument("--json", action="store_true", help="Emit JSON summary on stdout")
    args = p.parse_args()

    default_name, bindings = vaults.load_bindings()
    vault_name = args.vault or default_name
    if vault_name not in bindings:
        print(f"error: unknown vault {vault_name!r}", file=sys.stderr)
        return 1
    binding = bindings[vault_name].resolved()
    vault_roots = {vid: b.resolved().root for vid, b in bindings.items()}

    out = hygiene_apply.hygiene_folder(
        binding.root,
        folder=args.folder,
        vault_name=vault_name,
        vault_roots=vault_roots,
        fix_frontmatter=not args.no_frontmatter,
        fix_links=not args.no_links,
        fix_dialect=not args.no_dialect,
        dry_run=args.dry_run,
        limit=args.limit,
    )
    if not out.get("ok"):
        print(f"error: {out.get('error')}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        fm = sum(len(f.get("frontmatter") or []) for f in out.get("files") or [])
        links = sum(len(f.get("links") or []) for f in out.get("files") or [])
        dialect = sum(len(f.get("dialect") or []) for f in out.get("files") or [])
        print(
            f"vault={vault_name} folder={out.get('folder') or '/'} "
            f"scanned={out.get('scanned')} changed={out.get('changed')} "
            f"dry_run={out.get('dry_run')}"
        )
        print(f"  frontmatter_keys={fm} link_fixes={links} dialect_stubs={dialect}")
        for entry in (out.get("files") or [])[:20]:
            print(f"  {entry['path']}: fm={entry.get('frontmatter')} links={entry.get('links')} dialect={entry.get('dialect')}")
        rest = len(out.get("files") or []) - 20
        if rest > 0:
            print(f"  ... and {rest} more")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
