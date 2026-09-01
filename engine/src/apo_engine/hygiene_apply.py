"""Mechanical corpus hygiene fixes (frontmatter floor, cross-vault wikilinks, dialect stubs).

Used by ``engine/scripts/hygiene_batch.py`` — writes vault notes on disk like
trailing-whitespace auto-fix in :func:`note_lint.apply_auto_fixes`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from . import note_lint
from . import vaults as vault_reg
from .okf.frontmatter import set_fields

_WIKILINK_RE = re.compile(r"\[\[([^\]]+?)\]\]")
_H1_RE = re.compile(r"(?m)^#\s+(.+)$")
_DEFAULT_STATUS = "active"

# Hub link when usage-contract requires wikilinks but note has none.
_FOLDER_WIKILINK_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("areas/threads/", "[[areas/threads/sources]]"),
    ("areas/", "[[areas/threads/sources]]"),
    ("projects/", "[[areas/threads/sources]]"),
)


def default_wikilink_stub(path: str) -> str:
    rel = path.replace("\\", "/").strip("/")
    for prefix, link in _FOLDER_WIKILINK_DEFAULTS:
        if rel.startswith(prefix):
            return link
    return "[[areas/threads/sources]]"


def derive_title(content: str, path: str) -> str:
    m = _H1_RE.search(content)
    if m and m.group(1).strip():
        return m.group(1).strip()
    stem = Path(path).stem.replace("-", " ").replace("_", " ")
    return stem[:1].upper() + stem[1:]


def apply_frontmatter_floor(content: str, path: str, *, vault_root: Path) -> tuple[str, list[str]]:
    """Fill missing usage-contract frontmatter_floor keys (title, status)."""
    usage = vault_reg._read_usage_data(vault_root)  # noqa: SLF001
    floor = usage.get("frontmatter_floor") if usage else None
    if not isinstance(floor, list):
        return content, []
    name = Path(path).name
    parent = Path(path).parent.as_posix()
    if name in ("index.md", "log.md") and parent not in (".", ""):
        return content, []
    scalars = note_lint._parse_fm_scalars(content, path)  # noqa: SLF001
    updates: dict[str, str] = {}
    applied: list[str] = []
    for key in floor:
        k = str(key).strip()
        if not k:
            continue
        val = scalars.get(k)
        if val is not None and (not isinstance(val, str) or val.strip()):
            continue
        if k == "title":
            updates[k] = derive_title(content, path)
            applied.append("title")
        elif k == "status":
            updates[k] = _DEFAULT_STATUS
            applied.append("status")
        else:
            continue
    if not updates:
        return content, []
    if not content.lstrip().startswith("---"):
        # Minimal frontmatter block for notes that lack one.
        lines = ["---"]
        for k, v in updates.items():
            lines.append(f"{k}: {v}")
        lines.append("---")
        lines.append("")
        return "\n".join(lines) + content.lstrip(), applied
    return set_fields(content, updates, rel_path=path), applied


def replace_wikilink_target(content: str, old_target: str, new_target: str) -> tuple[str, bool]:
    """Replace wikilink targets matching ``old_target`` (ignores display alias)."""
    changed = False

    def repl(m: re.Match[str]) -> str:
        nonlocal changed
        inner = m.group(1)
        raw = inner.split("|", 1)[0].strip().split("#", 1)[0].strip().removesuffix(".md")
        if raw.lower() != old_target.lower():
            return m.group(0)
        changed = True
        if "|" in inner:
            display = inner.split("|", 1)[1]
            return f"[[{new_target}|{display}]]"
        anchor = ""
        if "#" in inner.split("|", 1)[0]:
            anchor = "#" + inner.split("#", 1)[1]
        return f"[[{new_target}{anchor}]]"

    new_content = _WIKILINK_RE.sub(repl, content)
    return new_content, changed


def apply_foreign_wikilink_fixes(
    content: str,
    *,
    path: str,
    vault: str,
    vault_root: Path,
    vault_roots: dict[str, Path] | None,
    foreign_idx_cache: dict[str, dict[str, list[str]]] | None = None,
) -> tuple[str, list[str]]:
    """Prefix bare wikilinks that resolve uniquely in a sibling registered vault."""
    if vault_roots is None:
        return content, []
    local_idx = note_lint.get_wiki_index(vault_root)
    cache = foreign_idx_cache if foreign_idx_cache is not None else {}
    applied: list[str] = []
    text = content
    for raw_target, _lineno in note_lint._wiki_targets(text):  # noqa: SLF001
        if note_lint._VAULT_PREFIX_RE.match(raw_target):  # noqa: SLF001
            continue
        if note_lint._wikilink_candidates(  # noqa: SLF001
            raw_target, local_idx, vault_root, exclude_path=path
        ):
            continue
        match = note_lint.resolve_foreign_wikilink(
            raw_target,
            current_vault=vault,
            vault_roots=vault_roots,
            foreign_idx_cache=cache,
        )
        if match is None:
            continue
        vid, _rel = match
        new_target = f"{vid}:{raw_target}"
        text, ok = replace_wikilink_target(text, raw_target, new_target)
        if ok:
            applied.append(f"{raw_target}→{new_target}")
    return text, applied


def apply_dialect_wikilink_stub(content: str, path: str) -> tuple[str, list[str]]:
    """Insert a default hub wikilink when dialect requires one and body has none."""
    if "[[" in content:
        return content, []
    stub = default_wikilink_stub(path)
    if content.lstrip().startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            rest = parts[2]
            if not rest.strip():
                return content, []
            if not rest.startswith("\n"):
                rest = "\n" + rest
            return f"---{parts[1]}---\n\n{stub}\n{rest.lstrip()}", ["wikilink_stub"]
    if not content.strip():
        return content, []
    return f"{stub}\n\n{content.lstrip()}", ["wikilink_stub"]


def apply_hygiene_to_note(
    content: str,
    *,
    path: str,
    vault: str,
    vault_root: Path,
    vault_roots: dict[str, Path] | None = None,
    foreign_idx_cache: dict[str, dict[str, list[str]]] | None = None,
    fix_frontmatter: bool = True,
    fix_links: bool = True,
    fix_dialect: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Return (new_content, report dict with applied fix lists)."""
    report: dict[str, Any] = {
        "frontmatter": [],
        "links": [],
        "dialect": [],
    }
    text = content
    if fix_frontmatter:
        text, fm = apply_frontmatter_floor(text, path, vault_root=vault_root)
        report["frontmatter"] = fm
    if fix_links:
        text, links = apply_foreign_wikilink_fixes(
            text,
            path=path,
            vault=vault,
            vault_root=vault_root,
            vault_roots=vault_roots,
            foreign_idx_cache=foreign_idx_cache,
        )
        report["links"] = links
    if fix_dialect:
        text, dialect = apply_dialect_wikilink_stub(text, path)
        report["dialect"] = dialect
    return text, report


def hygiene_folder(
    vault_root: Path,
    *,
    folder: str = "",
    vault_name: str = "",
    vault_roots: dict[str, Path] | None = None,
    fix_frontmatter: bool = True,
    fix_links: bool = True,
    fix_dialect: bool = True,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Walk ``folder`` and apply hygiene fixes to ``*.md`` notes."""
    folder_n = (folder or "").replace("\\", "/").strip().strip("/")
    base = vault_root / folder_n if folder_n else vault_root
    if not base.exists():
        return {"ok": False, "error": "folder_not_found", "folder": folder_n}
    foreign_idx_cache: dict[str, dict[str, list[str]]] = {}
    changed: list[dict[str, Any]] = []
    scanned = 0
    for p in sorted(base.rglob("*.md")):
        if not p.is_file():
            continue
        try:
            rel = str(p.relative_to(vault_root)).replace("\\", "/")
        except ValueError:
            continue
        if any(part.startswith(".") for part in Path(rel).parts):
            continue
        if rel.startswith("system/contracts/") or rel.startswith("system/config/"):
            continue
        scanned += 1
        if limit is not None and scanned > limit:
            break
        try:
            content = p.read_text(encoding="utf-8")
        except OSError:
            continue
        new_content, report = apply_hygiene_to_note(
            content,
            path=rel,
            vault=vault_name,
            vault_root=vault_root,
            vault_roots=vault_roots,
            foreign_idx_cache=foreign_idx_cache,
            fix_frontmatter=fix_frontmatter,
            fix_links=fix_links,
            fix_dialect=fix_dialect,
        )
        if new_content == content:
            continue
        entry = {"path": rel, **report}
        changed.append(entry)
        if not dry_run:
            p.write_text(new_content, encoding="utf-8")
    return {
        "ok": True,
        "folder": folder_n,
        "vault": vault_name,
        "scanned": scanned,
        "changed": len(changed),
        "files": changed,
        "dry_run": dry_run,
    }
