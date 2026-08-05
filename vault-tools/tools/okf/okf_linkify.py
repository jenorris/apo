#!/usr/bin/env python3
"""
okf_linkify.py — Append ## OKF links with bundle-relative URLs.

Requires vault OKF contract. Pass --vault / VAULT_ROOT.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from _bootstrap import bootstrap, split_vault_args

import okf_common
from okf_common import (
    bundle_url,
    concept_title,
    iter_markdown,
    rel_to_vault,
    split_frontmatter,
)

OKF_LINKS_RE = re.compile(r"(?ms)^## OKF links\s*\n.*?(?=^## |\Z)")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]")
RELATED_LINE_RE = re.compile(r"""['"]?\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]['"]?""")


def resolve_wikilink(link: str, from_path: Path) -> Path | None:
    link = link.strip().strip("'\"")
    # Already vault-relative with .md
    candidates = []
    if link.endswith(".md"):
        candidates.append(okf_common.VAULT_ROOT / link.lstrip("/"))
    else:
        candidates.append(okf_common.VAULT_ROOT / f"{link}.md")
        candidates.append(okf_common.VAULT_ROOT / link / f"{Path(link).name}.md")
    # Relative to note
    candidates.append((from_path.parent / f"{link}.md").resolve())
    candidates.append((from_path.parent / link).resolve())
    for c in candidates:
        if c.is_file():
            return c
    return None


def related_targets(scalars: dict[str, str], body: str, path: Path) -> list[Path]:
    targets: list[Path] = []
    seen: set[Path] = set()

    # related: list items appear in raw file; re-read related block simply via body+fm text
    text = path.read_text(encoding="utf-8", errors="replace")
    fm_match = re.match(r"\A---\r?\n(.*?)\r?\n---", text, re.DOTALL)
    block = fm_match.group(1) if fm_match else ""
    for line in block.splitlines():
        if "[[" not in line:
            continue
        for m in RELATED_LINE_RE.finditer(line):
            resolved = resolve_wikilink(m.group(1), path)
            if resolved and resolved.resolve() not in seen:
                seen.add(resolved.resolve())
                targets.append(resolved)

    # Connections / body wikilinks (cap)
    for m in WIKILINK_RE.finditer(body):
        if len(targets) >= 12:
            break
        resolved = resolve_wikilink(m.group(1), path)
        if resolved and resolved.resolve() not in seen and resolved.resolve() != path.resolve():
            seen.add(resolved.resolve())
            targets.append(resolved)

    return targets


def build_okf_links(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    scalars, body, _ = split_frontmatter(text)
    title = concept_title(path, scalars, body)
    lines = [
        "## OKF links",
        "",
        f"- This concept: [{title}]({bundle_url(path)})",
    ]
    for target in related_targets(scalars, body, path):
        t_text = target.read_text(encoding="utf-8", errors="replace")
        t_scalars, t_body, _ = split_frontmatter(t_text)
        t_title = concept_title(target, t_scalars, t_body)
        lines.append(f"- [{t_title}]({bundle_url(target)})")
    lines.append("")
    return "\n".join(lines)


def linkify(path: Path) -> bool:
    if path.name in {"index.md", "log.md"}:
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    section = build_okf_links(path)
    if OKF_LINKS_RE.search(text):
        new = OKF_LINKS_RE.sub(section.rstrip() + "\n", text)
    else:
        new = text.rstrip() + "\n\n" + section
    if new != text:
        path.write_text(new if new.endswith("\n") else new + "\n", encoding="utf-8")
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    root, raw = bootstrap(raw)
    _, raw = split_vault_args(raw)

    parser = argparse.ArgumentParser(description="Append ## OKF links with bundle-relative URLs")
    parser.add_argument("paths", nargs="*", help="Files or directories (default: none — require paths)")
    args = parser.parse_args(raw)

    if not args.paths:
        print("okf_linkify: pass one or more paths (file or directory)", file=sys.stderr)
        return 2

    targets = [Path(p) if Path(p).is_absolute() else root / p for p in args.paths]
    files: list[Path] = []
    for t in targets:
        files.extend(iter_markdown(t))

    changed = 0
    for path in files:
        if path.name in {"index.md", "log.md"}:
            continue
        if linkify(path):
            changed += 1
            print(f"linkify: {rel_to_vault(path)}")
    print(f"updated {changed} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
