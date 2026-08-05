#!/usr/bin/env python3
"""
okf_lint.py — Lint / fix / regenerate indexes for an OKF vault.

Requires vault system/contracts/okf-contract*.yaml.

  VAULT_ROOT=~/Notes/Meta python3 okf_lint.py [--strict] [path…]
  python3 okf_lint.py --vault ~/Notes/Meta --fix [path…]
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
    first_heading,
    is_bundle_root_index,
    is_reserved,
    iter_markdown,
    map_okf_type,
    rel_to_vault,
    set_scalar_in_frontmatter,
    should_skip_dir,
    split_frontmatter,
    strip_frontmatter,
    utc_now,
)
from lib.vault_env import resolve_under_vault



def lint_path(path: Path, strict: bool) -> list[str]:
    errors: list[str] = []
    warnings: list[str] = []
    rel = rel_to_vault(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{rel}: unreadable ({exc})"]

    scalars, body, has_fm = split_frontmatter(text)

    if path.name == "index.md":
        if is_bundle_root_index(path):
            if has_fm and not scalars.get("okf_version"):
                warnings.append(f"{rel}: root index.md should declare okf_version: \"0.1\"")
        elif has_fm:
            errors.append(f"{rel}: non-root index.md must not have frontmatter")
        return errors if strict else errors + [f"WARN {w}" for w in warnings]

    if path.name == "log.md":
        return []

    if not has_fm:
        errors.append(f"{rel}: missing YAML frontmatter")
        return errors

    if not (scalars.get("okf_type") or scalars.get("type")):
        errors.append(f"{rel}: missing type / okf_type")

    if strict:
        if not scalars.get("title") and not first_heading(body):
            errors.append(f"{rel}: missing title (and no H1)")
        if not (scalars.get("description") or first_heading(body)):
            errors.append(f"{rel}: missing description (and no H1 to derive)")
        if not (
            scalars.get("timestamp")
            or scalars.get("updated")
            or scalars.get("ingested_at")
            or scalars.get("date")
        ):
            errors.append(f"{rel}: missing timestamp/updated/ingested_at/date")

    return errors + ([f"WARN {w}" for w in warnings] if not strict else [])


def fix_path(path: Path) -> bool:
    """Apply safe auto-fixes. Returns True if file changed."""
    text = path.read_text(encoding="utf-8")
    original = text

    if path.name == "index.md" and not is_bundle_root_index(path):
        scalars, body, has_fm = split_frontmatter(text)
        if has_fm:
            # Only strip when body already looks like a listing, or FM is tiny metadata.
            listing_like = bool(
                re_search_listing(body)
                or len(body.strip()) < 40
            )
            if listing_like:
                text = strip_frontmatter(text)
                if not text.endswith("\n"):
                    text += "\n"
                if text != original:
                    path.write_text(text, encoding="utf-8")
                    return True
        return False

    if is_reserved(path):
        if is_bundle_root_index(path):
            scalars, _, has_fm = split_frontmatter(text)
            if has_fm and not scalars.get("okf_version"):
                text = set_scalar_in_frontmatter(text, {"okf_version": "0.1"})
                if text != original:
                    path.write_text(text, encoding="utf-8")
                    return True
        return False

    scalars, body, has_fm = split_frontmatter(text)
    updates: dict[str, str] = {}

    if not has_fm:
        # Minimal concept scaffold
        title = first_heading(body) or path.stem
        updates = {
            "title": title,
            "type": "note",
            "okf_type": map_okf_type(path, "note", None) or "Note",
            "description": title,
            "timestamp": utc_now(),
        }
        text = set_scalar_in_frontmatter(text, updates)
        path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        return True

    if not scalars.get("title"):
        h1 = first_heading(body)
        if h1:
            updates["title"] = h1

    if not scalars.get("description"):
        desc = concept_description(scalars, body)
        if desc:
            updates["description"] = desc

    if not (
        scalars.get("timestamp")
        or scalars.get("updated")
        or scalars.get("ingested_at")
        or scalars.get("date")
    ):
        updates["timestamp"] = utc_now()

    if not scalars.get("okf_type"):
        mapped = map_okf_type(path, scalars.get("type"), None)
        if mapped:
            updates["okf_type"] = mapped

    if not updates:
        return False

    text = set_scalar_in_frontmatter(text, updates)
    if text != original:
        path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        return True
    return False


def re_search_listing(body: str) -> bool:
    import re

    return bool(
        re.search(r"(?m)^##\s+(Concepts|Subdirectories)\b", body)
        or re.search(r"(?m)^\*\s+\[[^\]]+\]\([^)]+\)", body)
    )


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
    parser.add_argument("--strict", action="store_true", help="Fail on missing recommended fields")
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
    if not targets:
        targets = [root]

    if args.regenerate_indexes:
        dirs = dirs_for_indexes(targets, recursive=not args.no_recursive_indexes)
        written = 0
        for d in dirs:
            regenerate_index(d)
            written += 1
            print(f"index: {rel_to_vault(d / 'index.md')}")
        print(f"regenerated {written} index.md file(s)")
        return 0

    files: list[Path] = []
    for t in targets:
        files.extend(iter_markdown(t))

    # De-dupe preserving order; drop anything outside vault (symlink escape)
    seen: set[Path] = set()
    uniq: list[Path] = []
    for f in files:
        rp = f.resolve()
        if rp in seen:
            continue
        try:
            rp.relative_to(root)
        except ValueError:
            print(f"vault-tools: skip path outside vault: {f}", file=sys.stderr)
            continue
        seen.add(rp)
        uniq.append(f)

    fixed = 0
    all_issues: list[str] = []
    for path in uniq:
        if args.fix:
            if fix_path(path):
                fixed += 1
                print(f"fixed: {rel_to_vault(path)}")
        issues = lint_path(path, strict=args.strict)
        all_issues.extend(issues)

    for issue in all_issues:
        print(issue)

    errors = [i for i in all_issues if not i.startswith("WARN ")]
    print(
        f"scanned {len(uniq)} markdown file(s); "
        f"{'fixed ' + str(fixed) + '; ' if args.fix else ''}"
        f"{len(errors)} error(s), {len(all_issues) - len(errors)} warning(s)"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
