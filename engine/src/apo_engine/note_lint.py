"""Corpus lint detectors — structured ``flaws[]`` for library-scribe.

Channel split (normative): ``tip`` = habits · ``warning`` = ops · ``flaws`` = corpus quality.
See docs/library-scribe.md.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

from . import vaults as vault_reg

Remediation = Literal["auto", "llm", "human"]
Severity = Literal["info", "warn", "error"]

_CALLOUT_RE = re.compile(r"^>\s*\[!", re.M)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+?)\]\]")
_VAULT_PREFIX_RE = re.compile(r"^([a-zA-Z0-9][a-zA-Z0-9_-]{0,63}):(?!/)(.+)$")
# Prose mentions of a skill by name — either backticked ("`foo` skill") or bare
# ("the foo skill") — the two shapes actually used across this vault's skills
# and Hermes skill docs. Deliberately narrow to avoid false positives on
# ordinary sentences containing the word "skill".
_SKILL_MENTION_RE = re.compile(
    r"`([a-z0-9][a-z0-9._-]{1,63})`\s+skill\b"
    r"|\bthe\s+([A-Za-z][A-Za-z0-9._-]{1,63})\s+skill\b",
)


@dataclass
class Flaw:
    code: str
    severity: Severity
    path: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    remediation: Remediation = "llm"
    suggested_op: dict[str, Any] | None = None
    vault: str | None = None
    status: str | None = None  # e.g. "fixed"

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if out.get("suggested_op") is None:
            del out["suggested_op"]
        if out.get("vault") is None:
            del out["vault"]
        if out.get("status") is None:
            del out["status"]
        return out


def flaws_from_okf(
    okf_result: Any,
    *,
    path: str,
    vault: str = "",
) -> list[dict[str, Any]]:
    """Map soft OKF violations / leftover soft warnings into structured flaws.

    Hard rejects stay ``ok: false`` — callers should not invoke this on hard fail.
    Dual-emit: keep prose ``warnings`` on the response; also emit ``flaws``.
    """
    if not getattr(okf_result, "ok", True):
        return []
    enf = getattr(okf_result, "enforcement", "off") or "off"
    flaws: list[Flaw] = []
    violations = list(getattr(okf_result, "violations", None) or [])
    # Compat: recover from prose warnings when soft path cleared violations.
    if not violations:
        for w in getattr(okf_result, "warnings", None) or []:
            m = re.match(r"missing (\S+)", str(w))
            if m:
                violations.append({"field": m.group(1), "expected": "non-empty"})
    for v in violations:
        if not isinstance(v, dict):
            continue
        fld = str(v.get("field") or "").strip()
        if not fld:
            continue
        expected = str(v.get("expected") or "non-empty")
        is_type_mismatch = (
            fld in ("okf_type", "type")
            and expected not in ("non-empty", "")
            and not expected.startswith("ISO")
        )
        code = "okf.type_mismatch" if is_type_mismatch else "okf.missing_field"
        rem: Remediation = "human" if is_type_mismatch and enf == "hard" else "llm"
        suggested = {
            "tool": "patch_note",
            "ops": [{"op": "set_field", "field": fld, "value": None}],
        }
        msg = (
            f"okf_type mismatch (expected {expected})"
            if is_type_mismatch
            else f"missing {fld} (expected {expected})"
        )
        flaws.append(
            Flaw(
                code=code,
                severity="warn",
                path=path,
                vault=vault or None,
                evidence={"field": fld, "expected": expected},
                remediation=rem,
                suggested_op=suggested,
                message=msg,
            )
        )
    return [f.as_dict() for f in flaws]


def _trailing_ws_lines(content: str) -> list[int]:
    bad: list[int] = []
    for i, line in enumerate(content.splitlines(), 1):
        if line != line.rstrip(" \t"):
            bad.append(i)
    if content and not content.endswith("\n"):
        bad.append(len(content.splitlines()) or 1)
    return bad


def detect_trailing_ws(
    content: str,
    *,
    path: str,
    vault: str = "",
) -> list[Flaw]:
    lines = _trailing_ws_lines(content)
    if not lines:
        return []
    return [
        Flaw(
            code="format.trailing_ws",
            severity="info",
            path=path,
            vault=vault or None,
            evidence={"lines": lines[:20], "line_count": len(lines)},
            remediation="auto",
            message="trailing whitespace or missing final newline",
        )
    ]


def apply_trailing_ws_fix(content: str) -> str:
    """Strip trailing spaces/tabs per line; ensure a final newline when non-empty."""
    if not content:
        return content
    lines = content.splitlines()
    fixed = "\n".join(line.rstrip(" \t") for line in lines)
    if content.endswith("\n") or lines:
        fixed += "\n"
    return fixed


def apply_auto_fixes(
    content: str,
    *,
    path: str,
    vault: str = "",
    enabled: bool = True,
) -> tuple[str, list[dict[str, Any]]]:
    """Apply mechanical auto remediations before write_text.

    Returns (possibly rewritten content, flaws with status=fixed when applied).
    """
    if not enabled:
        return content, [f.as_dict() for f in detect_trailing_ws(content, path=path, vault=vault)]
    found = detect_trailing_ws(content, path=path, vault=vault)
    if not found:
        return content, []
    fixed = apply_trailing_ws_fix(content)
    if fixed == content:
        return content, [f.as_dict() for f in found]
    out_flaws: list[dict[str, Any]] = []
    for f in found:
        d = f.as_dict()
        d["status"] = "fixed"
        out_flaws.append(d)
    return fixed, out_flaws


def _parse_fm_scalars(content: str, rel: str) -> dict[str, Any]:
    try:
        from . import okf as apo_okf

        return apo_okf._parse_scalars(content, rel)  # noqa: SLF001
    except Exception:
        return {}


def detect_usage_frontmatter_floor(
    content: str,
    *,
    path: str,
    vault_root: Path,
    vault: str = "",
) -> list[Flaw]:
    usage = vault_reg._read_usage_data(vault_root)  # noqa: SLF001
    if not usage:
        return []
    floor = usage.get("frontmatter_floor")
    if not isinstance(floor, list) or not floor:
        return []
    # Skip reserved listing files (non-root index.md / log.md)
    name = Path(path).name
    parent = Path(path).parent.as_posix()
    if name in ("index.md", "log.md") and parent not in (".", ""):
        return []
    scalars = _parse_fm_scalars(content, path)
    flaws: list[Flaw] = []
    for key in floor:
        k = str(key).strip()
        if not k:
            continue
        val = scalars.get(k)
        if val is None or (isinstance(val, str) and not val.strip()):
            flaws.append(
                Flaw(
                    code="usage.frontmatter_floor",
                    severity="warn",
                    path=path,
                    vault=vault or None,
                    evidence={"field": k, "expected": "non-empty"},
                    remediation="llm",
                    suggested_op={
                        "tool": "patch_note",
                        "ops": [{"op": "set_field", "field": k, "value": None}],
                    },
                    message=f"usage frontmatter_floor missing {k}",
                )
            )
    return flaws


def detect_usage_dialect(
    content: str,
    *,
    path: str,
    vault_root: Path,
    vault: str = "",
) -> list[Flaw]:
    usage = vault_reg._read_usage_data(vault_root)  # noqa: SLF001
    if not usage:
        return []
    contrib = usage.get("contribution")
    if not isinstance(contrib, dict):
        return []
    features = contrib.get("features")
    if not isinstance(features, dict):
        return []
    flaws: list[Flaw] = []
    callouts = str(features.get("callouts") or "").strip().lower()
    if callouts == "never" and _CALLOUT_RE.search(content):
        flaws.append(
            Flaw(
                code="usage.dialect_feature",
                severity="warn",
                path=path,
                vault=vault or None,
                evidence={"feature": "callouts", "rule": "never"},
                remediation="llm",
                message="callout used but contribution.features.callouts is never",
            )
        )
    wikilinks = str(features.get("wikilinks") or "").strip().lower()
    if wikilinks == "required" and "[[" not in content and path.endswith(".md"):
        # only flag concept-ish notes with a body
        body = content
        if body.lstrip().startswith("---"):
            parts = body.split("---", 2)
            body = parts[2] if len(parts) >= 3 else body
        if len(body.strip()) > 80:
            flaws.append(
                Flaw(
                    code="usage.dialect_feature",
                    severity="info",
                    path=path,
                    vault=vault or None,
                    evidence={"feature": "wikilinks", "rule": "required"},
                    remediation="llm",
                    message="wikilinks required by usage contribution but none found",
                )
            )
    return flaws


def detect_layout_folder(
    *,
    path: str,
    vault_root: Path,
    vault: str = "",
) -> list[Flaw]:
    usage = vault_reg._read_usage_data(vault_root)  # noqa: SLF001
    if not usage:
        return []
    layout = usage.get("layout")
    if not isinstance(layout, dict) or not layout:
        return []
    rel = path.replace("\\", "/").strip("/")
    if not rel or "/" not in rel:
        return []
    top = rel.split("/", 1)[0]
    allowed = {str(k).strip() for k in layout if str(k).strip()}
    # Always allow system / archives common roots even if omitted
    allowed |= {"system", "archives", ".apo"}
    if top in allowed:
        return []
    return [
        Flaw(
            code="layout.unexpected_folder",
            severity="info",
            path=path,
            vault=vault or None,
            evidence={"folder": top, "layout_keys": sorted(allowed)},
            remediation="llm",
            message=f"path top-level {top!r} not in usage layout",
        )
    ]


def _wiki_targets(content: str) -> list[tuple[str, int]]:
    """Return (raw target, line) for each [[wiki-link]]."""
    out: list[tuple[str, int]] = []
    for lineno, line in enumerate(content.splitlines(), 1):
        for m in _WIKILINK_RE.finditer(line):
            raw = m.group(1).split("|", 1)[0].strip()
            raw = raw.split("#", 1)[0].strip().removesuffix(".md")
            if raw:
                out.append((raw, lineno))
    return out


def _build_wiki_index(vault_root: Path) -> dict[str, list[str]]:
    """Map lowercased stem / relative key → list of vault-relative paths."""
    index: dict[str, list[str]] = {}
    for p in vault_root.rglob("*.md"):
        if not p.is_file():
            continue
        try:
            rel = str(p.relative_to(vault_root)).replace("\\", "/")
        except ValueError:
            continue
        if any(part.startswith(".") for part in Path(rel).parts):
            continue
        key = rel[:-3].lower() if rel.endswith(".md") else rel.lower()
        stem = Path(rel).stem.lower()
        index.setdefault(key, []).append(rel)
        index.setdefault(stem, []).append(rel)
        # also basename without path
        if "/" in key:
            index.setdefault(key.rsplit("/", 1)[-1], []).append(rel)
    return index


def detect_broken_links(
    content: str,
    *,
    path: str,
    vault_root: Path,
    vault: str = "",
    wiki_index: dict[str, list[str]] | None = None,
    vault_roots: dict[str, Path] | None = None,
) -> list[Flaw]:
    """Broken/ambiguous [[wikilink]] detection.

    A target may carry a ``vault_id:rel`` prefix (the qualified_path form used
    throughout search/read/write hits). When ``vault_roots`` is supplied
    (vault_id -> root), a prefixed target resolves against *that* vault's own
    index instead of the local one; an unknown vault_id is its own flaw
    (``link.unknown_vault``) rather than a silent no-match.
    """
    idx = wiki_index if wiki_index is not None else _build_wiki_index(vault_root)
    foreign_idx_cache: dict[str, dict[str, list[str]]] = {}
    flaws: list[Flaw] = []
    seen: set[str] = set()
    for raw_target, lineno in _wiki_targets(content):
        dedupe_key = raw_target.replace("\\", "/").strip().lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        target_vault: str | None = None
        target = raw_target
        m = _VAULT_PREFIX_RE.match(raw_target)
        if m:
            target_vault, target = m.group(1), m.group(2)

        if target_vault is not None:
            if not vault_roots or target_vault not in vault_roots:
                flaws.append(
                    Flaw(
                        code="link.unknown_vault",
                        severity="warn",
                        path=path,
                        vault=vault or None,
                        evidence={"target": raw_target, "line": lineno, "unknown_vault": target_vault},
                        remediation="llm",
                        message=f"wikilink [[{raw_target}]] references unknown vault {target_vault!r}",
                    )
                )
                continue
            foreign_root = vault_roots[target_vault]
            if target_vault not in foreign_idx_cache:
                foreign_idx_cache[target_vault] = _build_wiki_index(foreign_root)
            use_idx = foreign_idx_cache[target_vault]
            use_root = foreign_root
        else:
            use_idx = idx
            use_root = vault_root

        key = target.replace("\\", "/").strip().lower()
        candidates = use_idx.get(key) or use_idx.get(key.rsplit("/", 1)[-1]) or []
        # unique paths
        uniq = sorted(set(candidates))
        # exclude self (only meaningful for same-vault links)
        if target_vault is None:
            uniq = [c for c in uniq if c != path]
        if not uniq:
            # also try exact file
            if (use_root / f"{target}.md").is_file() or (use_root / target).is_file():
                continue
            flaws.append(
                Flaw(
                    code="link.broken",
                    severity="warn",
                    path=path,
                    vault=vault or None,
                    evidence={
                        "target": raw_target,
                        "line": lineno,
                        "candidates": [],
                        **({"resolved_vault": target_vault} if target_vault else {}),
                    },
                    remediation="llm",
                    message=f"broken wikilink [[{raw_target}]]",
                )
            )
        elif len(uniq) > 1:
            flaws.append(
                Flaw(
                    code="link.ambiguous",
                    severity="warn",
                    path=path,
                    vault=vault or None,
                    evidence={
                        "target": raw_target,
                        "line": lineno,
                        "candidates": uniq[:10],
                        **({"resolved_vault": target_vault} if target_vault else {}),
                    },
                    remediation="llm",
                    message=f"ambiguous wikilink [[{raw_target}]] ({len(uniq)} targets)",
                )
            )
    return flaws


def detect_skill_references(
    content: str,
    *,
    path: str,
    vault: str = "",
    known_skills: Iterable[str] | None = None,
) -> list[Flaw]:
    """Flag prose that names a skill (`` `foo` skill `` / "the foo skill") not
    present in ``known_skills``.

    Opt-in: when ``known_skills`` is None (caller has no skill inventory to
    check against), this is a no-op — never guesses at what skills exist.
    Catches the class of bug where a skill/note tells the agent to "see the X
    skill" after X was renamed or removed, and nothing ever re-checks it.
    """
    if known_skills is None:
        return []
    known = {str(s).strip().lower() for s in known_skills if str(s).strip()}
    flaws: list[Flaw] = []
    seen: set[str] = set()
    for lineno, line in enumerate(content.splitlines(), 1):
        for m in _SKILL_MENTION_RE.finditer(line):
            name = (m.group(1) or m.group(2) or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in known or key in seen:
                continue
            seen.add(key)
            flaws.append(
                Flaw(
                    code="link.unknown_skill",
                    severity="warn",
                    path=path,
                    vault=vault or None,
                    evidence={"name": name, "line": lineno},
                    remediation="llm",
                    message=f"references {name!r} skill, not found in known_skills",
                )
            )
    return flaws


def detect_read_contract_type_mismatches(
    vault_root: Path,
    *,
    vault: str = "",
) -> list[Flaw]:
    """Cross-check ``okf_type`` references between okf-contract and read-contract.

    Vault-level (not per-note): flags read-contract ``type_authority`` /
    ``lifecycle_read`` / ``purposes`` / ``traversals`` entries that name an
    ``okf_type`` absent from okf-contract ``path_rules`` (``warn`` — a dangling
    reference), and the reverse — a ``path_rules`` okf_type with no
    read-contract ``type_authority``/``lifecycle_read`` entry (``info`` — an
    onboarding gap, not a defect). No-op when either contract is missing or
    unparsed, so a vault without a read-contract lints byte-identical to today.
    """
    from . import vault_contracts

    found = vault_contracts.discover_contracts(vault_root)
    read_entry = found.get("read-contract")
    if not read_entry or not read_entry.get("ok", True):
        return []
    read_data = read_entry.get("data") if isinstance(read_entry.get("data"), dict) else {}
    if not read_data:
        return []
    okf_entry = found.get("okf-contract")
    if not okf_entry or not okf_entry.get("ok", True):
        return []
    okf_data = okf_entry.get("data") if isinstance(okf_entry.get("data"), dict) else {}

    okf_types: set[str] = set()
    for rule in okf_data.get("path_rules") or []:
        if isinstance(rule, dict):
            t = str(rule.get("okf_type") or "").strip()
            if t:
                okf_types.add(t)

    read_types: set[str] = set()
    authority = read_data.get("type_authority")
    if isinstance(authority, dict):
        read_types.update(str(k).strip() for k in authority if str(k).strip())
    lifecycle = read_data.get("lifecycle_read")
    if isinstance(lifecycle, dict):
        read_types.update(str(k).strip() for k in lifecycle if str(k).strip())
    purposes = read_data.get("purposes")
    if isinstance(purposes, dict):
        for prow in purposes.values():
            if not isinstance(prow, dict):
                continue
            entry = prow.get("entry")
            if isinstance(entry, dict):
                t = str(entry.get("okf_type") or "").strip()
                if t:
                    read_types.add(t)
            for t in prow.get("read_order") or []:
                if isinstance(t, str) and t.strip():
                    read_types.add(t.strip())
    for edge in read_data.get("traversals") or []:
        if isinstance(edge, dict):
            for key in ("from", "to"):
                t = str(edge.get(key) or "").strip()
                if t:
                    read_types.add(t)

    rc_path = str(read_entry.get("path") or "system/contracts/read-contract.schema.yaml")
    okf_path = str(okf_entry.get("path") or "system/contracts/okf-contract.schema.yaml")

    flaws: list[Flaw] = []
    for t in sorted(read_types - okf_types):
        flaws.append(
            Flaw(
                code="read_contract.unknown_okf_type",
                severity="warn",
                path=rc_path,
                vault=vault or None,
                evidence={"okf_type": t},
                remediation="human",
                message=f"read-contract references okf_type {t!r}, absent from okf-contract path_rules",
            )
        )
    for t in sorted(okf_types - read_types):
        flaws.append(
            Flaw(
                code="read_contract.missing_type_authority",
                severity="info",
                path=okf_path,
                vault=vault or None,
                evidence={"okf_type": t},
                remediation="human",
                message=f"okf_type {t!r} in path_rules has no read-contract type_authority/lifecycle_read entry",
            )
        )
    return flaws


def lint_note(
    content: str,
    *,
    path: str,
    vault_root: Path,
    vault: str = "",
    include_links: bool = False,
    include_usage: bool = True,
    include_format: bool = True,
    wiki_index: dict[str, list[str]] | None = None,
    vault_roots: dict[str, Path] | None = None,
    known_skills: Iterable[str] | None = None,
    auto_fix: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """Run detectors for one note. Optionally auto-fix trailing WS.

    Returns (content_after_auto_fix, flaws).
    """
    flaws_out: list[dict[str, Any]] = []
    text = content
    if include_format:
        if auto_fix:
            text, fixed = apply_auto_fixes(text, path=path, vault=vault, enabled=True)
            flaws_out.extend(fixed)
        else:
            flaws_out.extend(
                f.as_dict() for f in detect_trailing_ws(text, path=path, vault=vault)
            )
        from apo_engine.note_format import is_mmd_note

        if is_mmd_note(path) or "```mermaid" in text:
            from apo_engine.mermaid_validate import validate_mermaid_text

            flaws_out.extend(f.as_dict() for f in validate_mermaid_text(text, path))
    if include_usage:
        flaws_out.extend(
            f.as_dict()
            for f in detect_usage_frontmatter_floor(
                text, path=path, vault_root=vault_root, vault=vault
            )
        )
        flaws_out.extend(
            f.as_dict()
            for f in detect_usage_dialect(
                text, path=path, vault_root=vault_root, vault=vault
            )
        )
        flaws_out.extend(
            f.as_dict()
            for f in detect_layout_folder(
                path=path, vault_root=vault_root, vault=vault
            )
        )
    if include_links:
        flaws_out.extend(
            f.as_dict()
            for f in detect_broken_links(
                text,
                path=path,
                vault_root=vault_root,
                vault=vault,
                wiki_index=wiki_index,
                vault_roots=vault_roots,
            )
        )
        flaws_out.extend(
            f.as_dict()
            for f in detect_skill_references(
                text,
                path=path,
                vault=vault,
                known_skills=known_skills,
            )
        )
    return text, flaws_out


def lint_folder(
    vault_root: Path,
    *,
    folder: str = "",
    limit: int = 50,
    offset: int = 0,
    vault_name: str = "",
    include_links: bool = True,
    known_skills: Iterable[str] | None = None,
    fix: bool = False,
) -> dict[str, Any]:
    """Paginated corpus lint sweep (non-archival detectors)."""
    folder_n = (folder or "").replace("\\", "/").strip().strip("/")
    base = vault_root / folder_n if folder_n else vault_root
    if not base.exists():
        return {
            "ok": True,
            "action": "lint",
            "flaws": [],
            "counts_by_code": {},
            "has_more": False,
            "offset": offset,
            "limit": limit,
            "vault": vault_name,
            "folder": folder_n,
            "warning": f"folder not found: {folder_n}" if folder_n else None,
        }

    wiki_index = _build_wiki_index(vault_root) if include_links else None
    vault_roots: dict[str, Path] | None = None
    if include_links:
        try:
            _, bindings = vault_reg.load_bindings()
            vault_roots = {name: b.resolved().root for name, b in bindings.items()}
        except Exception:
            vault_roots = None
    all_flaws: list[dict[str, Any]] = []
    paths: list[Path] = []
    for pat in ("*.md", "*.yaml", "*.yml"):
        paths.extend(sorted(base.rglob(pat)))
    for p in paths:
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
        try:
            content = p.read_text(encoding="utf-8")
        except OSError:
            continue
        new_content, note_flaws = lint_note(
            content,
            path=rel,
            vault_root=vault_root,
            vault=vault_name,
            include_links=include_links,
            include_usage=True,
            include_format=True,
            wiki_index=wiki_index,
            vault_roots=vault_roots,
            known_skills=known_skills,
            auto_fix=fix,
        )
        if fix and new_content != content:
            try:
                p.write_text(new_content, encoding="utf-8")
            except OSError:
                pass
        all_flaws.extend(note_flaws)

    counts: dict[str, int] = {}
    for f in all_flaws:
        code = str(f.get("code") or "?")
        counts[code] = counts.get(code, 0) + 1

    sliced = all_flaws[offset : offset + limit] if limit else all_flaws[offset:]
    has_more = (offset + len(sliced)) < len(all_flaws)
    out: dict[str, Any] = {
        "ok": True,
        "action": "lint",
        "flaws": sliced,
        "counts_by_code": counts,
        "total_flaws": len(all_flaws),
        "has_more": has_more,
        "offset": offset,
        "limit": limit,
        "vault": vault_name,
        "folder": folder_n,
        "source": "note_lint",
    }
    return out


def merge_lint_results(*parts: dict[str, Any]) -> dict[str, Any]:
    """Merge archival + note_lint vault lint payloads."""
    flaws: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    warnings: list[str] = []
    vault = ""
    folder = ""
    for part in parts:
        if not part:
            continue
        vault = vault or str(part.get("vault") or "")
        folder = folder or str(part.get("folder") or "")
        for f in part.get("flaws") or []:
            if isinstance(f, dict):
                flaws.append(f)
                code = str(f.get("code") or "?")
                counts[code] = counts.get(code, 0) + 1
        w = part.get("warning")
        if w:
            warnings.append(str(w))
        for code, n in (part.get("counts_by_code") or {}).items():
            counts[str(code)] = counts.get(str(code), 0) + int(n)
    # Prefer recount from merged flaws if both supplied counts
    if flaws:
        counts = {}
        for f in flaws:
            code = str(f.get("code") or "?")
            counts[code] = counts.get(code, 0) + 1
    out: dict[str, Any] = {
        "ok": True,
        "action": "lint",
        "flaws": flaws,
        "counts_by_code": counts,
        "total_flaws": len(flaws),
        "has_more": any(bool(p.get("has_more")) for p in parts if p),
        "vault": vault,
        "folder": folder,
    }
    if warnings:
        out["warning"] = "; ".join(warnings)
    return out


def extract_flaws_metrics(result: Any) -> dict[str, int]:
    """Flags for tool-metrics from a tool result dict."""
    if not isinstance(result, dict):
        return {}
    flaws = result.get("flaws")
    if not isinstance(flaws, list) or not flaws:
        return {}
    emitted = 0
    auto_fixed = 0
    for f in flaws:
        if not isinstance(f, dict):
            continue
        emitted += 1
        if f.get("status") == "fixed":
            auto_fixed += 1
    return {
        "flaws_emitted": emitted,
        "flaws_auto_fixed": auto_fixed,
    }
