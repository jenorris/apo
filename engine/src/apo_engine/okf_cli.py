"""``apo-engine okf`` / ``apo engine okf`` — validate | fix | init | export | ingest |
linkify | indexes.

One OKF implementation. The old ``vault-tools/`` tree (its own frontmatter
parser and type map, plus the linkify and index-regen scripts) is gone; every
OKF command lives here and reads frontmatter through ``apo_engine.okf``.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apo_engine import okf, vaults
from apo_engine.atomic_io import atomic_write_text

DEFAULT_ROOTS = ("inbox", "projects", "areas", "resources", "archives", "system")
RESERVED_NAMES = ("index.md", "log.md")


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------


@dataclass
class ValidateSummary:
    profile: str = "apo"
    scanned: int = 0
    violations: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    contract: str | None = None

    @property
    def ok(self) -> bool:
        return not self.violations

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "profile": self.profile,
            "scanned": self.scanned,
            "contract": self.contract,
            "violations": self.violations,
            "warnings": self.warnings,
        }


def ignore_patterns(root: Path) -> list[str]:
    """Same ignore set the indexer uses, resolved against an explicit root.

    ``core._load_ignore`` reads ``vaults.notes_root()``, which requires a bound
    vault; these commands take a root directly (``--vault-root``, an ingested
    bundle), so the root-relative ``.indexignore`` is read here instead.
    """
    from apo_engine import config
    from apo_engine.note_format import DEFAULT_YAML_IGNORE

    patterns = [".git/*", ".obsidian/*", "*.excalidraw.md", *DEFAULT_YAML_IGNORE]
    for ignore_file in (config.IGNORE_FILE, root / ".indexignore"):
        if ignore_file.exists():
            for line in ignore_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    return patterns


def iter_notes(root: Path, paths: list[Path] | None = None):
    """Vault notes, honoring ``.indexignore`` / ``APO_IGNORE`` like the indexer."""
    from apo_engine import core

    ignore = ignore_patterns(root)
    for note in core._iter_notes(root, ignore):
        if paths:
            if not any(_is_within(note, p) for p in paths):
                continue
        yield note


def _is_within(note: Path, target: Path) -> bool:
    note = note.resolve()
    target = target.resolve()
    if target.is_file():
        return note == target
    return target == note or target in note.parents


def validate_vault(
    root: Path,
    *,
    profile: str = "apo",
    paths: list[Path] | None = None,
) -> ValidateSummary:
    summary = ValidateSummary(profile=profile)
    root = root.resolve()

    # Without a contract the engine is convention-agnostic and every check is a
    # no-op — which would otherwise report "0 violations" and hand someone a
    # false clean bill of health on a vault that is not an OKF bundle at all.
    contract_path = okf.resolve_contract_path(root)
    if contract_path is None:
        if profile == "okf":
            summary.violations.append(
                {
                    "path": ".",
                    "field": "contract",
                    "expected": "system/contracts/okf-contract.schema.yaml "
                    "(run `apo-engine okf init`)",
                }
            )
        else:
            summary.warnings.append(
                "no OKF contract found — OKF is off for this vault; "
                "run `apo-engine okf init` to adopt one"
            )
        return summary
    summary.contract = str(contract_path)

    for note in sorted(iter_notes(root, paths)):
        rel = note.relative_to(root).as_posix()
        try:
            content = note.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            summary.violations.append(
                {"path": rel, "field": "file", "expected": f"readable ({exc})"}
            )
            continue
        summary.scanned += 1
        report = okf.validate_concept(
            vault_root=root, rel_path=rel, content=content, profile=profile
        )
        for v in report.violations:
            summary.violations.append({**v, "path": rel})
        summary.warnings.extend(report.warnings)
    return summary


def format_validate(summary: ValidateSummary) -> str:
    lines = [
        f"{v['path']}: missing {v['field']} (expected {v['expected']})"
        for v in summary.violations
    ]
    lines.extend(f"WARN {w}" for w in summary.warnings)
    if summary.contract is None:
        lines.append(
            f"profile={summary.profile}: no OKF contract — nothing was checked"
        )
        return "\n".join(lines)
    lines.append(
        f"profile={summary.profile}: scanned {summary.scanned} note(s); "
        f"{len(summary.violations)} violation(s), {len(summary.warnings)} warning(s)"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# fix
# --------------------------------------------------------------------------


def fix_vault(
    root: Path,
    *,
    paths: list[Path] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Stamp missing core fields across a vault via the normal write path.

    This runs ``process_concept`` — the same contract-driven inference the MCP
    write path uses — so there is no second type map to drift. Reserved paths
    are left alone; a ``hard`` violation is reported, not force-written.
    """
    root = root.resolve()
    fixed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    scanned = 0

    for note in sorted(iter_notes(root, paths)):
        rel = note.relative_to(root).as_posix()
        try:
            content = note.read_text(encoding="utf-8")
        except OSError as exc:
            failed.append({"path": rel, "error": str(exc)})
            continue
        scanned += 1
        result = okf.process_concept(vault_root=root, rel_path=rel, content=content)
        if not result.ok:
            failed.append({"path": rel, "error": result.error, "message": result.message})
            continue
        if not result.stamped or result.content == content:
            continue
        if not dry_run:
            atomic_write_text(note, result.content)
        fixed.append({"path": rel, "stamped": result.stamped})

    return {
        "ok": not failed,
        "scanned": scanned,
        "fixed": fixed,
        "failed": failed,
        "dry_run": dry_run,
    }


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------

STARTER_CONTRACT = '''# OKF machine contract — see docs/contracts/okf-bundle.md
okf_version: "{version}"

# Apo's native typed field. `type` is the OKF interchange field (SPEC §11)
# and is emitted alongside it.
type_field: okf_type
legacy_type_field: type
spec_type_field: type
# fill | mirror | off — `fill` never overwrites an existing `type` value.
spec_type_policy: fill

# Producer profile: stricter than the spec on purpose.
core_required:
  - okf_type
  - description
  - timestamp

# Consumer profile: SPEC §11 requires exactly this.
spec_required:
  - type

core_soft:
  - title
  - resource

default_enforcement: soft
default_okf_type: Note

reserved_filenames:
  - index.md
  - log.md

path_rules:
  - match: "index.md"
    enforcement: exempt
    notes: "bundle root; may declare okf_version"
  - match: "**/index.md"
    enforcement: reserved
    notes: "OKF listing; no concept frontmatter"
  - match: "**/log.md"
    enforcement: reserved
    notes: "OKF changelog; no concept stamp"
'''

STARTER_INDEX = '''---
okf_version: "{version}"
title: "{title}"
description: "OKF knowledge bundle root"
---

# {title}

## Subdirectories
'''


def init_bundle(
    root: Path,
    *,
    okf_version: str = "0.1",
    force: bool = False,
) -> dict[str, Any]:
    """Scaffold a contract + bundle-root index. Never clobbers without ``force``."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        return {"ok": False, "error": "not_a_directory", "path": str(root)}

    created: list[str] = []
    skipped: list[str] = []

    contract = root / "system" / "contracts" / "okf-contract.schema.yaml"
    if contract.exists() and not force:
        skipped.append(str(contract.relative_to(root)))
    else:
        contract.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(contract, STARTER_CONTRACT.format(version=okf_version))
        created.append(str(contract.relative_to(root)))

    index = root / "index.md"
    if index.exists() and not force:
        skipped.append("index.md")
    else:
        atomic_write_text(index, STARTER_INDEX.format(version=okf_version, title=root.name))
        created.append("index.md")

    okf.clear_contract_cache()
    return {"ok": True, "root": str(root), "created": created, "skipped": skipped}


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------


def export_bundle(
    root: Path,
    dest: Path,
    *,
    roots: list[str] | None = None,
    okf_version: str = "0.1",
) -> dict[str, Any]:
    """Copy a conformant OKF subset of ``root`` into ``dest``.

    Concepts missing the spec's ``type`` get it filled from ``okf_type`` on the
    way out, so the exported bundle satisfies SPEC §11 even when the source
    vault predates dual-emission.
    """
    root = root.resolve()
    dest = dest.expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    include = list(roots or DEFAULT_ROOTS)

    present = [name for name in include if (root / name).is_dir()]
    stamp = okf.utc_now()
    atomic_write_text(
        dest / "index.md",
        "\n".join(
            [
                "---",
                f'okf_version: "{okf_version}"',
                'title: "OKF export"',
                f'description: "Exported from {root.name} at {stamp}"',
                f'timestamp: "{stamp}"',
                "---",
                "",
                "# OKF export",
                "",
                f"Exported {stamp} from `{root.name}`.",
                "",
                "## Subdirectories",
                "",
                *[f"* [{name}]({name}/)" for name in present],
                "",
            ]
        ),
    )

    copied = 0
    skipped = 0
    stamped_type = 0
    for name in present:
        for note in sorted(iter_notes(root, [root / name])):
            rel = note.relative_to(root).as_posix()
            text = note.read_text(encoding="utf-8", errors="replace")
            reserved = note.name in RESERVED_NAMES
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)

            if reserved:
                shutil.copy2(note, out)
                copied += 1
                continue

            meta = okf.read_concept(text, rel)
            if not okf.has_frontmatter(text, rel):
                skipped += 1
                continue
            if not (meta.type or meta.okf_type):
                skipped += 1
                continue
            if not meta.type and meta.okf_type:
                text = okf.set_fields(text, {"type": meta.okf_type}, rel_path=rel)
                stamped_type += 1
            atomic_write_text(out, text)
            copied += 1

    # Ship the contract so the export validates standalone.
    src_contract = okf.resolve_contract_path(root)
    if src_contract is not None:
        out_contract = dest / "system" / "contracts" / src_contract.name
        out_contract.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_contract, out_contract)

    atomic_write_text(
        dest / "export-manifest.txt",
        f"exported_at={stamp}\ncopied={copied}\nskipped={skipped}\n"
        f"stamped_type={stamped_type}\nokf_version={okf_version}\n"
        f"roots={','.join(present)}\n",
    )
    return {
        "ok": True,
        "dest": str(dest),
        "copied": copied,
        "skipped": skipped,
        "stamped_type": stamped_type,
    }


def archive_bundle(staging: Path, archive_path: Path) -> Path:
    arcname = archive_path.name
    for suffix in (".tar.gz", ".tgz"):
        if arcname.endswith(suffix):
            arcname = arcname[: -len(suffix)]
            break
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(staging, arcname=arcname)
    return archive_path


# --------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------


def ingest_bundle(
    src: Path,
    name: str,
    *,
    vaults_file: Path | None = None,
    index: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Register an external OKF bundle as a **read-only** vault.

    Copying a foreign bundle into the vault root would satisfy the "one vault
    root" boundary but pollute a personal vault with someone else's notes.
    Registering it as its own read-only vault keeps the boundary, reuses the
    existing multi-vault machinery, and is a more honest demonstration of
    consuming OKF: the bundle is searchable via ``vault=`` / ``vaults=[]`` and
    every write op against it fails with ``read_only_vault``.
    """
    src = src.expanduser().resolve()
    if not src.is_dir():
        return {"ok": False, "error": "not_a_directory", "path": str(src)}

    # A bundle must at least look like one before we mount it.
    summary = validate_vault(src, profile="okf")
    if not summary.ok and not force:
        return {
            "ok": False,
            "error": "not_conformant",
            "path": str(src),
            "violations": summary.violations[:10],
            "hint": "run `apo-engine okf validate --profile okf` on it, or pass --force",
        }

    target = (vaults_file or _vaults_file()).expanduser()
    if target is None:
        return {"ok": False, "error": "no_vaults_file", "hint": "set APO_VAULTS to a JSON path"}

    data: dict[str, Any] = {}
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": "bad_vaults_file", "message": str(exc)}
    registry = data.setdefault("vaults", {})
    if not isinstance(registry, dict):
        return {"ok": False, "error": "bad_vaults_file", "message": "`vaults` must be an object"}

    if name in registry and not force:
        return {"ok": False, "error": "vault_exists", "name": name, "hint": "pass --force to replace"}

    registry[name] = {
        "root": str(src),
        "index": str(index or (Path.home() / ".apo" / f"index-{name}.db")),
        "read_only": True,
    }
    data.setdefault("default", next(iter(registry)))

    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, json.dumps(data, indent=2) + "\n")

    return {
        "ok": True,
        "name": name,
        "root": str(src),
        "read_only": True,
        "vaults_file": str(target),
        "conformant": summary.ok,
        "violations": len(summary.violations),
        "next": f"apo-engine index --vault {name}",
    }


def _vaults_file() -> Path | None:
    import os

    raw = os.environ.get("APO_VAULTS", "").strip()
    if raw and not raw.lstrip().startswith("{"):
        return Path(raw)
    if raw:
        # Inline JSON — there is no file to amend.
        return None
    return Path.home() / ".apo" / "vaults.json"


# --------------------------------------------------------------------------
# linkify + index regen
# --------------------------------------------------------------------------
#
# Both are contract-gated batch mutators: they refuse a vault with no OKF
# contract (exit 2) and refuse any path that resolves outside the vault root.
# Neither one infers a concept type — titles and descriptions only.

PARA_ROOTS = ("inbox", "projects", "areas", "resources", "archives", "system")
SKIP_DIR_NAMES = frozenset(
    {".git", ".obsidian", ".cursor", "node_modules", "__pycache__", ".agents", "agent-client"}
)
_LISTING_DESC_MAX = 120
_LINKIFY_MAX_TARGETS = 12

_OKF_LINKS_RE = re.compile(r"(?ms)^## OKF links\s*\n.*?(?=^## |\Z)")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]")


class OkfCliError(Exception):
    """A usage / precondition failure; ``main`` prints it and exits ``code``."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def require_contract(root: Path) -> Path:
    """The vault's OKF contract path, or ``OkfCliError`` when there is none."""
    contract = okf.resolve_contract_path(root)
    if contract is None:
        raise OkfCliError(
            f"okf: vault has no OKF contract: {root}\n"
            "  expected system/contracts/okf-contract.schema.yaml "
            "(run `apo engine okf init`)"
        )
    return contract


def resolve_under_root(root: Path, path_arg: str | Path) -> Path:
    """Resolve a CLI path and require it stay under ``root`` (``..``/symlink escapes fail)."""
    root = root.resolve()
    raw = Path(path_arg).expanduser()
    candidate = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise OkfCliError(f"okf: path escapes vault root: {path_arg} (vault={root})") from None
    return candidate


def _skip_dir(path: Path, root: Path) -> bool:
    """True when ``path`` (a directory) sits in or under a skipped directory name."""
    try:
        parts = path.resolve().relative_to(root).parts
    except ValueError:
        parts = path.parts
    return bool(SKIP_DIR_NAMES.intersection(parts))


def _iter_markdown(root: Path, target: Path) -> list[Path]:
    if target.is_file():
        return [target] if target.suffix == ".md" else []
    if not target.exists():
        return []
    return [p for p in sorted(target.rglob("*.md")) if not _skip_dir(p.parent, root)]


def _concept_title(path: Path, text: str) -> str:
    return okf.parse_scalars(text).get("title") or okf.first_h1(text) or path.stem


def _concept_description(text: str) -> str | None:
    return okf.parse_scalars(text).get("description") or okf.first_h1(text)


def _bundle_url(root: Path, path: Path) -> str:
    try:
        return "/" + path.resolve().relative_to(root).as_posix()
    except ValueError:
        return "/" + path.as_posix()


def _rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _resolve_wikilink(root: Path, link: str, from_path: Path) -> Path | None:
    link = link.strip().strip("'\"")
    candidates: list[Path] = []
    if link.endswith(".md"):
        candidates.append(root / link.lstrip("/"))
    else:
        candidates.append(root / f"{link}.md")
        candidates.append(root / link / f"{Path(link).name}.md")
    candidates.append((from_path.parent / f"{link}.md").resolve())
    candidates.append((from_path.parent / link).resolve())
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _related_targets(root: Path, path: Path, text: str) -> list[Path]:
    targets: list[Path] = []
    seen: set[Path] = set()

    def add(link: str, *, skip_self: bool) -> None:
        resolved = _resolve_wikilink(root, link, path)
        if resolved is None:
            return
        key = resolved.resolve()
        if key in seen or (skip_self and key == path.resolve()):
            return
        seen.add(key)
        targets.append(resolved)

    # Frontmatter wikilinks (``related:`` items) come first, uncapped.
    fm = okf._FM_RE.match(text)
    for line in (fm.group(1) if fm else "").splitlines():
        if "[[" in line:
            for m in _WIKILINK_RE.finditer(line):
                add(m.group(1), skip_self=False)

    # Body wikilinks fill the remainder up to the cap.
    for m in _WIKILINK_RE.finditer(okf.body_of(text)):
        if len(targets) >= _LINKIFY_MAX_TARGETS:
            break
        add(m.group(1), skip_self=True)
    return targets


def build_okf_links(root: Path, path: Path, text: str | None = None) -> str:
    text = path.read_text(encoding="utf-8", errors="replace") if text is None else text
    lines = [
        "## OKF links",
        "",
        f"- This concept: [{_concept_title(path, text)}]({_bundle_url(root, path)})",
    ]
    for target in _related_targets(root, path, text):
        t_text = target.read_text(encoding="utf-8", errors="replace")
        lines.append(f"- [{_concept_title(target, t_text)}]({_bundle_url(root, target)})")
    lines.append("")
    return "\n".join(lines)


def linkify_note(root: Path, path: Path) -> bool:
    """Write/replace the ``## OKF links`` section. True when the file changed."""
    if path.name in RESERVED_NAMES:
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    section = build_okf_links(root, path, text)
    if _OKF_LINKS_RE.search(text):
        new = _OKF_LINKS_RE.sub(lambda _m: section.rstrip() + "\n", text)
    else:
        new = text.rstrip() + "\n\n" + section
    if not new.endswith("\n"):
        new += "\n"
    if new == text:
        return False
    atomic_write_text(path, new)
    return True


def linkify_vault(root: Path, paths: list[Path]) -> dict[str, Any]:
    """Linkify every markdown note under ``paths`` (files or directories)."""
    root = root.resolve()
    changed = [
        _rel(root, path)
        for target in paths
        for path in _iter_markdown(root, target)
        if linkify_note(root, path)
    ]
    return {"ok": True, "changed": changed}


def regenerate_index(directory: Path, root: Path) -> Path:
    """Write the OKF §6 ``index.md`` listing for one directory (no frontmatter)."""
    concepts: list[tuple[str, str, str]] = []
    subdirs: list[tuple[str, str]] = []

    for child in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
        if child.name.startswith("."):
            continue
        if child.is_dir():
            if _skip_dir(child, root):
                continue
            if any(child.rglob("*.md")):
                subdirs.append((child.name, f"{child.name}/"))
            continue
        if child.suffix != ".md" or child.name in RESERVED_NAMES:
            continue
        text = child.read_text(encoding="utf-8", errors="replace")
        title = _concept_title(child, text)
        concepts.append((title, child.name, _concept_description(text) or title))

    lines = [f"# {directory.name}", ""]
    if concepts:
        lines += ["## Concepts", ""]
        for title, name, desc in concepts:
            short = desc.replace("\n", " ").strip()
            if len(short) > _LISTING_DESC_MAX:
                short = short[: _LISTING_DESC_MAX - 3] + "…"
            lines.append(f"* [{title}]({name}) - {short}")
        lines.append("")
    if subdirs:
        lines += ["## Subdirectories", ""]
        lines += [f"* [{name}]({href})" for name, href in subdirs]
        lines.append("")

    out = directory / "index.md"
    atomic_write_text(out, "\n".join(lines).rstrip() + "\n")
    return out


def index_dirs(root: Path, paths: list[Path], *, recursive: bool = True) -> list[Path]:
    """Directories whose ``index.md`` should be regenerated."""
    root = root.resolve()
    roots = [p if p.is_dir() else p.parent for p in (paths or [root])]

    out: list[Path] = []
    seen: set[Path] = set()
    for base in roots:
        if base in seen or not base.is_dir():
            continue
        if recursive:
            for d in [base, *sorted(base.rglob("*"))]:
                if not d.is_dir() or _skip_dir(d, root) or d in seen:
                    continue
                children = list(d.iterdir())
                if any(c.suffix == ".md" and c.is_file() for c in children) or any(
                    c.is_dir() and not _skip_dir(c, root) for c in children
                ):
                    seen.add(d)
                    out.append(d)
        else:
            seen.add(base)
            out.append(base)
            if base == root:  # PARA roots: also one level of children
                for name in PARA_ROOTS:
                    child = base / name
                    if child.is_dir() and child not in seen:
                        seen.add(child)
                        out.append(child)
    return out


def regenerate_indexes(
    root: Path, paths: list[Path], *, recursive: bool = True
) -> dict[str, Any]:
    root = root.resolve()
    written = [
        _rel(root, regenerate_index(d, root))
        for d in index_dirs(root, paths, recursive=recursive)
    ]
    return {"ok": True, "written": written}


# --------------------------------------------------------------------------
# argparse wiring
# --------------------------------------------------------------------------


def _root_for(args) -> Path:
    """Vault root: explicit --vault-root wins, else the named/default vault."""
    explicit = (getattr(args, "vault_root", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    default, bindings = vaults.load_bindings()
    key = (getattr(args, "vault", "") or "").strip() or default
    if key not in bindings:
        raise SystemExit(f"unknown vault {key!r}; available: {sorted(bindings)}")
    return bindings[key].resolved().root


def _cmd_validate(args) -> int:
    root = _root_for(args)
    targets = [Path(p).expanduser().resolve() for p in (args.paths or [])]
    summary = validate_vault(root, profile=args.profile, paths=targets or None)
    if args.json:
        print(json.dumps(summary.as_dict(), indent=2))
    else:
        print(format_validate(summary))
    return 0 if summary.ok else 1


def _cmd_fix(args) -> int:
    root = _root_for(args)
    targets = [Path(p).expanduser().resolve() for p in (args.paths or [])]
    out = fix_vault(root, paths=targets or None, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        for entry in out["fixed"]:
            print(f"fixed: {entry['path']} ({', '.join(entry['stamped'])})")
        for entry in out["failed"]:
            print(f"FAILED {entry['path']}: {entry.get('message') or entry.get('error')}")
        verb = "would fix" if args.dry_run else "fixed"
        print(
            f"scanned {out['scanned']} note(s); {verb} {len(out['fixed'])}; "
            f"{len(out['failed'])} failure(s)"
        )
    return 0 if out["ok"] else 1


def _cmd_init(args) -> int:
    out = init_bundle(_root_for(args), okf_version=args.okf_version, force=args.force)
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


def _cmd_export(args) -> int:
    root = _root_for(args)
    roots = [r.strip() for r in (args.roots or "").split(",") if r.strip()] or None
    dest = Path(args.destination).expanduser()

    if args.archive:
        archive_path = dest if dest.suffixes else Path(str(dest) + ".tar.gz")
        staging = archive_path.parent / f".{archive_path.name}.staging"
        if staging.exists():
            shutil.rmtree(staging)
        out = export_bundle(root, staging, roots=roots, okf_version=args.okf_version)
        archive_bundle(staging, archive_path)
        shutil.rmtree(staging, ignore_errors=True)
        out["archive"] = str(archive_path)
        out.pop("dest", None)
    else:
        out = export_bundle(root, dest, roots=roots, okf_version=args.okf_version)

    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


def _cmd_ingest(args) -> int:
    out = ingest_bundle(
        Path(args.source),
        args.name,
        index=Path(args.index).expanduser() if args.index else None,
        force=args.force,
    )
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


def _guarded(fn):
    """Turn ``OkfCliError`` into a stderr message + exit code (works under ``apo engine`` too)."""

    def run(args) -> int:
        try:
            return fn(args)
        except OkfCliError as exc:
            print(exc, file=sys.stderr)
            return exc.code

    return run


def _cmd_linkify(args) -> int:
    root = _root_for(args)
    if not args.paths:
        raise OkfCliError("okf linkify: pass one or more paths (file or directory)")
    require_contract(root)
    targets = [resolve_under_root(root, p) for p in args.paths]
    out = linkify_vault(root, targets)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        for rel in out["changed"]:
            print(f"linkify: {rel}")
        print(f"updated {len(out['changed'])} file(s)")
    return 0


def _cmd_indexes(args) -> int:
    root = _root_for(args)
    require_contract(root)
    targets = [resolve_under_root(root, p) for p in args.paths]
    out = regenerate_indexes(root, targets, recursive=not args.no_recursive)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        for rel in out["written"]:
            print(f"index: {rel}")
        print(f"regenerated {len(out['written'])} index.md file(s)")
    return 0


def add_parser(sub) -> None:
    """Attach the ``okf`` subcommand group to an argparse subparsers object."""
    p = sub.add_parser("okf", help="OKF bundle: validate | init | export | ingest")
    okf_sub = p.add_subparsers(dest="okf_cmd", required=True)

    def _common(sp):
        sp.add_argument("--vault", default="", help="registered vault_id (default vault if omitted)")
        sp.add_argument(
            "--vault-root", default="", help="explicit vault root (overrides --vault)"
        )
        return sp

    pv = _common(okf_sub.add_parser("validate", help="check a vault against a conformance profile"))
    pv.add_argument("paths", nargs="*", help="files/dirs to check (default: whole vault)")
    pv.add_argument(
        "--profile",
        choices=("apo", "okf"),
        default="apo",
        help="apo = Apo producer profile (default); okf = SPEC §11 exactly",
    )
    pv.add_argument("--json", action="store_true")
    pv.set_defaults(func=_cmd_validate)

    pf = _common(okf_sub.add_parser("fix", help="stamp missing core fields via the normal write path"))
    pf.add_argument("paths", nargs="*", help="files/dirs to fix (default: whole vault)")
    pf.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    pf.add_argument("--json", action="store_true")
    pf.set_defaults(func=_cmd_fix)

    pn = _common(okf_sub.add_parser("init", help="scaffold an OKF contract + bundle root index"))
    pn.add_argument("--okf-version", choices=("0.1", "0.2"), default="0.1")
    pn.add_argument("--force", action="store_true", help="overwrite existing files")
    pn.set_defaults(func=_cmd_init)

    pe = _common(okf_sub.add_parser("export", help="export a conformant OKF subset"))
    pe.add_argument("destination", help="output directory, or archive path with --archive")
    pe.add_argument("--roots", default="", help=f"comma-separated roots (default: {','.join(DEFAULT_ROOTS)})")
    pe.add_argument("--okf-version", choices=("0.1", "0.2"), default="0.1")
    pe.add_argument("--archive", action="store_true", help="write a .tar.gz instead of a directory")
    pe.set_defaults(func=_cmd_export)

    pl = _common(
        okf_sub.add_parser("linkify", help="write/refresh `## OKF links` with bundle-relative URLs")
    )
    pl.add_argument("paths", nargs="*", help="files/dirs under the vault (required)")
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=_guarded(_cmd_linkify))

    pi = _common(okf_sub.add_parser("indexes", help="regenerate index.md listings (no frontmatter)"))
    pi.add_argument("paths", nargs="*", help="files/dirs under the vault (default: whole vault)")
    pi.add_argument(
        "--no-recursive",
        action="store_true",
        help="only the target roots (plus PARA roots at the vault root), not the whole tree",
    )
    pi.add_argument("--json", action="store_true")
    pi.set_defaults(func=_guarded(_cmd_indexes))

    pg = okf_sub.add_parser("ingest", help="register an external OKF bundle as a read-only vault")
    pg.add_argument("source", help="path to the external bundle root")
    pg.add_argument("--name", required=True, help="vault name to register it under")
    pg.add_argument("--index", default="", help="sqlite index path (default ~/.apo/index-<name>.db)")
    pg.add_argument("--force", action="store_true", help="replace an existing entry / skip conformance gate")
    pg.set_defaults(func=_cmd_ingest)


def main(argv: list[str] | None = None) -> int:
    """Entry point taking the ``okf`` subcommand directly, e.g. ``["validate", …]``."""
    import argparse

    p = argparse.ArgumentParser(prog="apo-engine okf")
    sub = p.add_subparsers(dest="cmd", required=True)
    add_parser(sub)
    args = p.parse_args(["okf", *(argv if argv is not None else sys.argv[1:])])
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
