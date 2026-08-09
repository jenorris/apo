#!/usr/bin/env python3
"""
okf_lint.py — shim over ``apo-engine okf validate`` / ``apo-engine okf fix``.

Conformance checking and stamping now live in ``apo_engine.okf``; this file
used to carry a second implementation (its own frontmatter parser and its own
``map_okf_type`` table) that could drift from the engine's contract-driven
inference. Only ``--regenerate-indexes`` is still implemented here — it builds
OKF §6 listings and involves no type logic, so there is nothing to drift.

  python3 okf_lint.py --vault ~/Notes/Meta [--profile okf] [--strict] [path…]
  python3 okf_lint.py --vault ~/Notes/Meta --fix [path…]
  python3 okf_lint.py --vault ~/Notes/Meta --regenerate-indexes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _bootstrap import bootstrap, split_vault_args

import okf_common
from okf_common import (
    PARA_ROOTS,
    concept_description,
    concept_title,
    rel_to_vault,
    should_skip_dir,
    split_frontmatter,
)
from okf_shim import run_engine_okf
from lib.vault_env import resolve_under_vault


def regenerate_index(directory: Path) -> Path:
    """Write OKF §6 index.md for a directory (no frontmatter)."""
    concepts: list[tuple[str, str, str]] = []
    subdirs: list[tuple[str, str]] = []

    for child in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
        if child.name.startswith("."):
            continue
        if child.is_dir():
            if should_skip_dir(child):
                continue
            # Prefer dirs that contain markdown or nested content
            if any(child.rglob("*.md")):
                subdirs.append((child.name, f"{child.name}/"))
            continue
        if child.suffix != ".md" or child.name in {"index.md", "log.md"}:
            continue
        text = child.read_text(encoding="utf-8", errors="replace")
        scalars, body, _ = split_frontmatter(text)
        title = concept_title(child, scalars, body)
        desc = concept_description(scalars, body) or title
        concepts.append((title, child.name, desc))

    lines = [f"# {directory.name}", ""]
    if concepts:
        lines.append("## Concepts")
        lines.append("")
        for title, name, desc in concepts:
            # Keep description short
            short = desc.replace("\n", " ").strip()
            if len(short) > 120:
                short = short[:117] + "…"
            lines.append(f"* [{title}]({name}) - {short}")
        lines.append("")
    if subdirs:
        lines.append("## Subdirectories")
        lines.append("")
        for name, href in subdirs:
            lines.append(f"* [{name}]({href})")
        lines.append("")

    out = directory / "index.md"
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out


def dirs_for_indexes(paths: list[Path], recursive: bool) -> list[Path]:
    if not paths:
        roots = [
            okf_common.VAULT_ROOT / name
            for name in PARA_ROOTS
            if (okf_common.VAULT_ROOT / name).is_dir()
        ]
        roots.append(okf_common.VAULT_ROOT)
    else:
        roots = []
        for p in paths:
            p = p.resolve()
            roots.append(p if p.is_dir() else p.parent)

    out: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if root in seen or not root.is_dir():
            continue
        if recursive:
            for d in [root, *sorted(root.rglob("*"))]:
                if not d.is_dir() or should_skip_dir(d) or d in seen:
                    continue
                # Only directories that contain at least one .md
                if any(c.suffix == ".md" and c.is_file() for c in d.iterdir() if c.is_file()) or any(
                    c.is_dir() and not should_skip_dir(c) for c in d.iterdir()
                ):
                    seen.add(d)
                    out.append(d)
        else:
            seen.add(root)
            out.append(root)
            # PARA roots: also one level of children when targeting vault root
            if root == okf_common.VAULT_ROOT.resolve():
                for name in PARA_ROOTS:
                    child = root / name
                    if child.is_dir() and child not in seen:
                        seen.add(child)
                        out.append(child)
    return out


def main(argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    root, raw = bootstrap(raw)
    _, raw = split_vault_args(raw)

    parser = argparse.ArgumentParser(description="OKF lint / fix / index regen (contract-gated)")
    parser.add_argument("paths", nargs="*", help="Files or directories (default: whole vault)")
    parser.add_argument("--strict", action="store_true", help="(accepted; the engine profile decides)")
    parser.add_argument(
        "--profile",
        choices=("apo", "okf"),
        default="apo",
        help="apo = Apo producer profile (default); okf = SPEC §11 conformance exactly",
    )
    parser.add_argument("--fix", action="store_true", help="Auto-fill safe frontmatter gaps")
    parser.add_argument(
        "--regenerate-indexes",
        action="store_true",
        help="Rewrite index.md listings (no frontmatter)",
    )
    parser.add_argument(
        "--no-recursive-indexes",
        action="store_true",
        help="With --regenerate-indexes, only target roots (not deep tree)",
    )
    args = parser.parse_args(raw)

    targets = [resolve_under_vault(root, p) for p in args.paths]

    if args.regenerate_indexes:
        dirs = dirs_for_indexes(targets or [root], recursive=not args.no_recursive_indexes)
        for d in dirs:
            regenerate_index(d)
            print(f"index: {rel_to_vault(d / 'index.md')}")
        print(f"regenerated {len(dirs)} index.md file(s)")
        return 0

    paths = [str(p) for p in targets]

    if args.fix:
        rc = run_engine_okf("fix", root, paths)
        if rc:
            return rc

    return run_engine_okf("validate", root, ["--profile", args.profile, *paths])


if __name__ == "__main__":
    raise SystemExit(main())
