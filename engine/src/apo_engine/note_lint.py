"""Corpus lint detectors — structured ``flaws[]`` for library-scribe.

Channel split (normative): ``tip`` = habits · ``warning`` = ops · ``flaws`` = corpus quality.
See docs/library-scribe.md.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import vaults as vault_reg

Remediation = Literal["auto", "llm", "human"]
Severity = Literal["info", "warn", "error"]

_CALLOUT_RE = re.compile(r"^>\s*\[!", re.M)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+?)\]\]")


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
) -> list[Flaw]:
    idx = wiki_index if wiki_index is not None else _build_wiki_index(vault_root)
    flaws: list[Flaw] = []
    seen: set[str] = set()
    for target, lineno in _wiki_targets(content):
        key = target.replace("\\", "/").strip().lower()
        if key in seen:
            continue
        seen.add(key)
        candidates = idx.get(key) or idx.get(key.rsplit("/", 1)[-1]) or []
        # unique paths
        uniq = sorted(set(candidates))
        # exclude self
        uniq = [c for c in uniq if c != path]
        if not uniq:
            # also try exact file
            if (vault_root / f"{target}.md").is_file() or (
                vault_root / target
            ).is_file():
                continue
            flaws.append(
                Flaw(
                    code="link.broken",
                    severity="warn",
                    path=path,
                    vault=vault or None,
                    evidence={"target": target, "line": lineno, "candidates": []},
                    remediation="llm",
                    message=f"broken wikilink [[{target}]]",
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
                        "target": target,
                        "line": lineno,
                        "candidates": uniq[:10],
                    },
                    remediation="llm",
                    message=f"ambiguous wikilink [[{target}]] ({len(uniq)} targets)",
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
