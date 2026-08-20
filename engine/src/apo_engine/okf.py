"""OKF write-path stamp / validate — vault-contract-driven, optional.

When no contract is configured or found, writes pass through unchanged (engine
stays convention-agnostic). Meta vault ships ``system/contracts/okf-contract.schema.yaml``
(legacy ``system/config/`` and ``okf-profile.schema.yaml`` still accepted).

See Meta ``system/config/apo-okf-write-contract.md`` and Apo ``docs/contracts/okf-bundle.md``.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from apo_engine.markdown_patch import (
    _set_field_lines,
    join_lines,
    normalize_lines,
)
from apo_engine.note_format import is_yaml_note, parse_yaml_document
from apo_engine.yaml_patch import set_yaml_fields

_H1_RE = re.compile(r"(?m)^#\s+(.+)$")
_FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
_SCALAR_RE = re.compile(r"^([A-Za-z0-9_]+):\s*(.*)$")

_CONTRACT_LOCK = threading.Lock()
_CONTRACT_CACHE: dict[str, tuple[float | None, "OkfContract | None"]] = {}
# Compiled glob patterns for path_rules (``**`` support on Py < 3.13).
_GLOB_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _compile_glob(pat: str) -> re.Pattern[str]:
    """Compile a POSIX path glob with ``**`` (recursive) support."""
    out: list[str] = ["^"]
    i = 0
    n = len(pat)
    while i < n:
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        elif pat[i] == "[":
            j = i + 1
            if j < n and pat[j] == "!":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape(pat[i]))
                i += 1
            else:
                cls = pat[i : j + 1]
                if cls.startswith("[!"):
                    cls = "[^" + cls[2:]
                out.append(cls)
                i = j + 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def path_glob_match(rel: str, pat: str) -> bool:
    """Match a vault-relative path against a glob (``**`` works on 3.11+).

    Uses ``PurePath.full_match`` on 3.13+; otherwise a ``**``-aware regex
    backport. Does not use ``fnmatch`` / ``Path.match`` — those treat ``*`` as
    crossing ``/`` or as a suffix match, which breaks OKF path_rules on CI.
    """
    rel = rel.replace("\\", "/").lstrip("/")
    pat = pat.replace("\\", "/")
    if not pat:
        return False
    path = PurePosixPath(rel)
    if hasattr(path, "full_match"):
        try:
            return bool(path.full_match(pat))
        except ValueError:
            return False
    compiled = _GLOB_RE_CACHE.get(pat)
    if compiled is None:
        compiled = _compile_glob(pat)
        _GLOB_RE_CACHE[pat] = compiled
    return compiled.fullmatch(rel) is not None


def match_rule(contract: OkfContract, rel_path: str) -> PathRule | None:
    rel = rel_path.replace("\\", "/").lstrip("/")
    path = PurePosixPath(rel)
    for rule in contract.path_rules:
        if path_glob_match(rel, rule.match):
            return rule
    name = path.name
    if name in contract.reserved_filenames and rel != "index.md":
        return PathRule(match=name, enforcement="reserved")
    if rel == "index.md":
        return PathRule(match="index.md", enforcement="exempt")
    return None


@dataclass
class PathRule:
    match: str
    enforcement: str = "soft"  # exempt | reserved | soft | hard
    okf_type: str | None = None
    required_fields: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class OkfContract:
    path: Path
    okf_version: str = "0.1"
    type_field: str = "okf_type"
    legacy_type_field: str = "type"
    core_required: list[str] = field(default_factory=lambda: ["okf_type", "description", "timestamp"])
    core_soft: list[str] = field(default_factory=lambda: ["title", "resource"])
    default_enforcement: str = "soft"
    default_okf_type: str = "Note"
    reserved_filenames: list[str] = field(default_factory=lambda: ["index.md", "log.md"])
    path_rules: list[PathRule] = field(default_factory=list)
    legacy_type_map: dict[str, str] = field(default_factory=dict)


@dataclass
class OkfResult:
    content: str
    stamped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    violations: list[dict[str, str]] = field(default_factory=list)
    okf_type: str | None = None
    enforcement: str = "off"  # off | exempt | reserved | soft | hard
    ok: bool = True
    error: str | None = None
    message: str | None = None

    def as_response_fields(self) -> dict[str, Any]:
        out: dict[str, Any] = {"enforcement": self.enforcement}
        if self.stamped:
            out["stamped"] = self.stamped
        if self.warnings:
            out["warnings"] = self.warnings
        if self.okf_type:
            out["okf_type"] = self.okf_type
        # Hard-reject payload only — soft misses become flaws[] (see note_lint).
        if self.violations and self.enforcement == "hard" and not self.ok:
            out["violations"] = self.violations
        return out


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_contract_path(vault_root: Path, explicit: str | None = None) -> Path | None:
    if explicit is None:
        explicit = (
            os.environ.get("APO_OKF_CONTRACT", "").strip()
            or os.environ.get("APO_OKF_PROFILE", "").strip()  # legacy alias
        )
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    names = ("okf-contract.schema.yaml", "okf-profile.schema.yaml")
    for base in (vault_root / "system" / "contracts", vault_root / "system" / "config"):
        for name in names:
            candidate = base / name
            if candidate.is_file():
                return candidate
    return None


def enforcement_override() -> str | None:
    raw = os.environ.get("APO_OKF_ENFORCEMENT", "").strip().lower()
    if raw in {"off", "soft", "hard"}:
        return raw
    return None


def _parse_scalars(text: str, rel_path: str = "") -> dict[str, str]:
    if is_yaml_note(rel_path):
        data = parse_yaml_document(text)
        if not data:
            return {}
        out: dict[str, str] = {}
        for key, val in data.items():
            if isinstance(val, (dict, list)):
                continue
            out[str(key)] = "" if val is None else str(val)
        return out
    m = _FM_RE.match(text)
    if not m:
        return {}
    scalars: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line or line.startswith((" ", "\t", "-", "#")):
            continue
        sm = _SCALAR_RE.match(line)
        if not sm:
            continue
        key, val = sm.group(1), sm.group(2).strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        scalars[key] = val
    return scalars


def _first_h1(text: str, rel_path: str = "") -> str | None:
    if is_yaml_note(rel_path):
        return None
    body = _FM_RE.sub("", text, count=1) if _FM_RE.match(text) else text
    m = _H1_RE.search(body)
    return m.group(1).strip() if m else None


def _has_frontmatter(text: str, rel_path: str = "") -> bool:
    if is_yaml_note(rel_path):
        return parse_yaml_document(text) is not None
    return bool(_FM_RE.match(text))


def load_contract(path: Path) -> OkfContract:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"OKF contract must be a mapping: {path}")

    rules: list[PathRule] = []
    for raw in data.get("path_rules") or []:
        if not isinstance(raw, dict) or not raw.get("match"):
            continue
        req = raw.get("required_fields") or []
        if not isinstance(req, list):
            req = []
        rules.append(
            PathRule(
                match=str(raw["match"]),
                enforcement=str(raw.get("enforcement") or "soft").lower(),
                okf_type=str(raw["okf_type"]) if raw.get("okf_type") else None,
                required_fields=[str(x) for x in req],
                notes=str(raw.get("notes") or ""),
            )
        )

    legacy = data.get("legacy_type_map") or {}
    if not isinstance(legacy, dict):
        legacy = {}

    return OkfContract(
        path=path,
        okf_version=str(data.get("okf_version") or "0.1"),
        type_field=str(data.get("type_field") or "okf_type"),
        legacy_type_field=str(data.get("legacy_type_field") or "type"),
        core_required=[str(x) for x in (data.get("core_required") or ["okf_type", "description", "timestamp"])],
        core_soft=[str(x) for x in (data.get("core_soft") or ["title", "resource"])],
        default_enforcement=str(data.get("default_enforcement") or "soft").lower(),
        default_okf_type=str(data.get("default_okf_type") or "Note"),
        reserved_filenames=[str(x) for x in (data.get("reserved_filenames") or ["index.md", "log.md"])],
        path_rules=rules,
        legacy_type_map={str(k): str(v) for k, v in legacy.items()},
    )


def get_contract(vault_root: Path) -> OkfContract | None:
    """Load and cache contract for vault_root. None = OKF off for this vault."""
    contract_path = resolve_contract_path(vault_root)
    if contract_path is None:
        return None
    key = str(vault_root.resolve())
    try:
        mtime = contract_path.stat().st_mtime
    except OSError:
        return None
    with _CONTRACT_LOCK:
        cached = _CONTRACT_CACHE.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            contract = load_contract(contract_path)
        except (OSError, ValueError, yaml.YAMLError):
            _CONTRACT_CACHE[key] = (mtime, None)
            return None
        _CONTRACT_CACHE[key] = (mtime, contract)
        return contract


def clear_contract_cache() -> None:
    with _CONTRACT_LOCK:
        _CONTRACT_CACHE.clear()


def _effective_enforcement(rule_enforcement: str, contract_default: str) -> str:
    override = enforcement_override()
    if override == "off":
        return "off"
    base = rule_enforcement or contract_default or "soft"
    if base in {"exempt", "reserved"}:
        return base
    if override in {"soft", "hard"}:
        return override
    return base if base in {"soft", "hard"} else "soft"


def _infer_okf_type(contract: OkfContract, rel_path: str, scalars: dict[str, str], rule: PathRule | None) -> str:
    type_field = contract.type_field
    existing = (scalars.get(type_field) or "").strip()
    if existing:
        return existing
    if rule and rule.okf_type:
        return rule.okf_type
    legacy = (scalars.get(contract.legacy_type_field) or "").strip()
    if legacy and legacy in contract.legacy_type_map:
        return contract.legacy_type_map[legacy]
    return contract.default_okf_type


def _set_fields(content: str, updates: dict[str, str], *, rel_path: str = "") -> str:
    if not updates:
        return content
    if is_yaml_note(rel_path):
        return set_yaml_fields(content, updates)
    had_nl = content.endswith("\n")
    lines = normalize_lines(content)
    for key, val in updates.items():
        lines = _set_field_lines(lines, key, val)
    return join_lines(lines, had_nl)


def process_concept(
    *,
    vault_root: Path,
    rel_path: str,
    content: str,
    bump_timestamp: bool = False,
) -> OkfResult:
    """Stamp / validate concept content. No-op when contract missing or enforcement off."""
    override = enforcement_override()
    if override == "off":
        return OkfResult(content=content, enforcement="off")

    contract = get_contract(vault_root)
    if contract is None:
        return OkfResult(content=content, enforcement="off")

    rel = rel_path.replace("\\", "/").lstrip("/")
    rule = match_rule(contract, rel)
    rule_enf = rule.enforcement if rule else contract.default_enforcement
    enf = _effective_enforcement(rule_enf, contract.default_enforcement)

    if enf == "off":
        return OkfResult(content=content, enforcement="off")

    if enf == "reserved":
        warnings: list[str] = []
        violations: list[dict[str, str]] = []
        if _has_frontmatter(content, rel):
            msg = f"reserved path {rel!r} must not have concept frontmatter"
            warnings.append(msg)
            violations.append({"field": "frontmatter", "expected": "absent"})
            return OkfResult(
                content=content,
                warnings=warnings,
                violations=violations,
                enforcement="reserved",
                ok=False,
                error="okf_validation",
                message=msg,
            )
        return OkfResult(content=content, enforcement="reserved", warnings=warnings)

    if enf == "exempt":
        stamped: list[str] = []
        new_content = content
        scalars = _parse_scalars(content, rel)
        # May stamp timestamp only
        if "timestamp" not in scalars or not scalars["timestamp"].strip():
            new_content = _set_fields(new_content, {"timestamp": utc_now()}, rel_path=rel)
            stamped.append("timestamp")
        elif bump_timestamp:
            new_content = _set_fields(new_content, {"timestamp": utc_now()}, rel_path=rel)
            stamped.append("timestamp")
        okf = _infer_okf_type(contract, rel, _parse_scalars(new_content, rel), rule)
        return OkfResult(
            content=new_content,
            stamped=stamped,
            okf_type=okf if _has_frontmatter(new_content, rel) else None,
            enforcement="exempt",
        )

    # soft / hard
    scalars = _parse_scalars(content, rel)
    updates: dict[str, str] = {}
    stamped = []
    warnings = []
    type_field = contract.type_field

    inferred = _infer_okf_type(contract, rel, scalars, rule)
    if not (scalars.get(type_field) or "").strip():
        updates[type_field] = inferred
        stamped.append(type_field)

    h1 = _first_h1(content, rel)
    stem = Path(rel).stem

    if not (scalars.get("description") or "").strip():
        desc = h1 or (scalars.get("title") or "").strip() or stem
        updates["description"] = desc
        stamped.append("description")
        if h1:
            warnings.append("missing description (derived from H1)")
        else:
            warnings.append("missing description (derived from title/stem)")

    if bump_timestamp or not (scalars.get("timestamp") or "").strip():
        # Also accept legacy date fields as present (no stamp) unless bump requested
        if bump_timestamp or not any(
            (scalars.get(k) or "").strip() for k in ("timestamp", "updated", "ingested_at", "date")
        ):
            updates["timestamp"] = utc_now()
            stamped.append("timestamp")

    if not (scalars.get("title") or "").strip():
        title = h1 or stem
        updates["title"] = title
        stamped.append("title")

    if not (scalars.get("resource") or "").strip():
        src = (scalars.get("source_url") or scalars.get("source") or "").strip()
        if src and (src.startswith("http://") or src.startswith("https://") or "://" in src):
            updates["resource"] = src
            stamped.append("resource")

    # Never overwrite existing non-empty okf_type / resource — only set if missing (above)

    new_content = _set_fields(content, updates, rel_path=rel)
    final_scalars = _parse_scalars(new_content, rel)
    okf_type = (final_scalars.get(type_field) or inferred).strip() or None

    required = list(contract.core_required)
    if rule and rule.required_fields:
        for f in rule.required_fields:
            if f not in required:
                required.append(f)

    violations: list[dict[str, str]] = []
    for f in required:
        if f == type_field:
            val = (final_scalars.get(type_field) or "").strip()
            if not val:
                violations.append({"field": type_field, "expected": rule.okf_type or contract.default_okf_type})
            elif rule and rule.okf_type and val != rule.okf_type and enf == "hard":
                violations.append({"field": type_field, "expected": rule.okf_type})
            continue
        if f == "timestamp":
            if not any(
                (final_scalars.get(k) or "").strip() for k in ("timestamp", "updated", "ingested_at", "date")
            ):
                violations.append({"field": "timestamp", "expected": "ISO-8601"})
            continue
        if not (final_scalars.get(f) or "").strip():
            violations.append({"field": f, "expected": "non-empty"})

    if violations and enf == "hard":
        msg = "; ".join(f"{v['field']} (expected {v['expected']})" for v in violations)
        return OkfResult(
            content=new_content,
            stamped=stamped,
            warnings=warnings,
            violations=violations,
            okf_type=okf_type,
            enforcement="hard",
            ok=False,
            error="okf_validation",
            message=msg,
        )

    if violations and enf == "soft":
        for v in violations:
            warnings.append(f"missing {v['field']} (expected {v['expected']})")

    return OkfResult(
        content=new_content,
        stamped=stamped,
        warnings=warnings,
        # Keep soft violations for flaws[] dual-emit; as_response_fields only
        # surfaces ``violations`` on hard reject.
        violations=violations,
        okf_type=okf_type,
        enforcement=enf,
        ok=True,
    )


def _classify_and_check(
    contract: OkfContract, rel: str, scalars: dict[str, str], rule: PathRule | None
) -> tuple[str, list[str], list[dict[str, str]]]:
    """Given a contract + a note's on-disk scalars, return
    (okf_type, required_fields, violations) — read-only, mirrors the
    classification/validation half of ``process_concept`` without stamping
    or mutating content, so it's safe to run against notes that are not
    being written right now (e.g. a dry-run sweep).
    """
    type_field = contract.type_field
    inferred = _infer_okf_type(contract, rel, scalars, rule)
    okf_type = (scalars.get(type_field) or "").strip() or inferred

    required = list(contract.core_required)
    if rule and rule.required_fields:
        for f in rule.required_fields:
            if f not in required:
                required.append(f)

    violations: list[dict[str, str]] = []
    for f in required:
        if f == type_field:
            if not (scalars.get(type_field) or "").strip():
                violations.append({
                    "field": type_field,
                    "expected": rule.okf_type if rule and rule.okf_type else contract.default_okf_type,
                })
            continue
        if f == "timestamp":
            if not any(
                (scalars.get(k) or "").strip() for k in ("timestamp", "updated", "ingested_at", "date")
            ):
                violations.append({"field": "timestamp", "expected": "ISO-8601"})
            continue
        if not (scalars.get(f) or "").strip():
            violations.append({"field": f, "expected": "non-empty"})
    return okf_type, required, violations


# Fields process_concept auto-derives on write (H1/title/stem/utc_now) — a
# violation on one of these self-heals the next time the note is written,
# unlike a custom required_field which has no derivation and needs a real
# value supplied. Surfaced as `auto_fillable` in okf_dry_run output rather
# than silently excluded, so the reviewer can judge either way.
_AUTO_FILLABLE_FIELDS = {"okf_type", "description", "timestamp", "title"}


def _sample_path_for_glob(pat: str) -> str:
    """Generate one concrete path that `pat` would match, for shadow-testing.

    Not a formal glob-containment proof — a practical heuristic: if the
    sample also matches an earlier rule, that earlier rule wins first-match
    and the later rule is dead for at least this shape of path.
    """
    pat = pat.replace("\\", "/")
    out: list[str] = []
    i = 0
    n = len(pat)
    seg = 0
    while i < n:
        if pat.startswith("**/", i):
            out.append(f"sample{seg}/sample{seg + 1}/")
            seg += 2
            i += 3
        elif pat.startswith("**", i):
            out.append(f"sample{seg}/sample{seg + 1}")
            seg += 2
            i += 2
        elif pat[i] == "*":
            out.append(f"sample{seg}")
            seg += 1
            i += 1
        elif pat[i] == "?":
            out.append("x")
            i += 1
        elif pat[i] == "[":
            j = pat.find("]", i)
            out.append("x")
            i = j + 1 if j != -1 else i + 1
        else:
            out.append(pat[i])
            i += 1
    return "".join(out)


def _find_shadowed_rules(path_rules: list[PathRule]) -> list[dict[str, str]]:
    """Rules whose glob is fully pre-matched by an earlier rule (first-match-wins dead code)."""
    shadowed: list[dict[str, str]] = []
    for idx, rule in enumerate(path_rules):
        sample = _sample_path_for_glob(rule.match)
        for earlier in path_rules[:idx]:
            if path_glob_match(sample, earlier.match):
                shadowed.append({
                    "match": rule.match,
                    "shadowed_by": earlier.match,
                    "sample_path": sample,
                })
                break
    return shadowed


def okf_dry_run(vault_root: Path, vault_name: str, contract_yaml: str) -> dict[str, Any]:
    """Simulate a proposed OKF contract against the current corpus. Read-only —
    never writes anything, never touches the live contract file on disk.

    Reports:
      reclassified   — notes whose okf_type would change under the proposal
      newly_flawed   — notes that pass the live contract but would gain a
                        required-field violation under the proposal
      shadowed_rules — proposed path_rules dead on arrival under first-match-wins
    """
    import tempfile

    try:
        yaml.safe_load(contract_yaml)
    except yaml.YAMLError as exc:
        return {"ok": False, "error": "bad_request", "message": f"invalid contract YAML: {exc}"}

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".okf_dry_run.yaml", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(contract_yaml)
            tmp_path = Path(tmp.name)
        try:
            proposed = load_contract(tmp_path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            return {"ok": False, "error": "bad_request", "message": f"invalid contract: {exc}"}
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    live = get_contract(vault_root)  # None if OKF off for this vault
    shadowed_rules = _find_shadowed_rules(proposed.path_rules)

    reclassified: list[dict[str, str]] = []
    newly_flawed: list[dict[str, Any]] = []
    scanned = 0

    paths: list[Path] = []
    for pat in ("*.md", "*.yaml", "*.yml"):
        paths.extend(sorted(vault_root.rglob(pat)))
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
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        scanned += 1
        scalars = _parse_scalars(text, rel)

        new_rule = match_rule(proposed, rel)
        new_type, _new_req, new_violations = _classify_and_check(proposed, rel, scalars, new_rule)

        if live is not None:
            old_rule = match_rule(live, rel)
            old_type, _old_req, old_violations = _classify_and_check(live, rel, scalars, old_rule)
        else:
            old_type, old_violations = None, []

        if old_type != new_type:
            reclassified.append({"path": rel, "old_okf_type": old_type, "new_okf_type": new_type})

        old_fields = {v["field"] for v in old_violations}
        new_only = [v for v in new_violations if v["field"] not in old_fields]
        if new_only:
            newly_flawed.append({
                "path": rel,
                "okf_type": new_type,
                "violations": [
                    {**v, "auto_fillable": v["field"] in _AUTO_FILLABLE_FIELDS} for v in new_only
                ],
            })

    return {
        "ok": True,
        "action": "okf_dry_run",
        "vault": vault_name,
        "notes_scanned": scanned,
        "reclassified": reclassified,
        "newly_flawed": newly_flawed,
        "shadowed_rules": shadowed_rules,
        "live_okf_enabled": live is not None,
    }


# Back-compat aliases (pre-contracts rename)
resolve_profile_path = resolve_contract_path
load_profile = load_contract
get_profile = get_contract
clear_profile_cache = clear_contract_cache
OkfProfile = OkfContract
