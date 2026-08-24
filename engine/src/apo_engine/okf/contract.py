"""OKF contract loading, path rules, and glob matching.

The contract is a per-vault YAML file (``system/contracts/okf-contract.schema.yaml``).
When absent the engine stays convention-agnostic and OKF is off for that vault.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import yaml

# Compiled glob patterns for path_rules (``**`` support on Py < 3.13).
_GLOB_RE_CACHE: dict[str, re.Pattern[str]] = {}


_CONTRACT_LOCK = threading.Lock()
_CONTRACT_CACHE: dict[str, tuple[float | None, "OkfContract | None"]] = {}


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
    # --- OKF interchange conformance (SPEC §11) -------------------------------
    # SPEC requires a non-empty ``type`` on every concept frontmatter block.
    # Apo's native field is ``okf_type``; ``type`` is emitted alongside it so a
    # stamped vault is a conformant bundle. Unknown extra keys are explicitly
    # tolerated by SPEC §11 consumer obligations, so dual-emission is spec-legal.
    spec_type_field: str = "type"
    # fill   — write ``type`` only when absent/empty (preserves legacy taxonomies)
    # mirror — always force ``type`` to the resolved OKF type
    # off    — never emit ``type``
    spec_type_policy: str = "fill"
    # Consumer/spec validation profile: SPEC §11 requires exactly this much.
    spec_required: list[str] = field(default_factory=lambda: ["type"])
    # --- v0.2 producer provenance (SPEC §5, §13.1) ---------------------------
    # off     — emit nothing (default; v0.1 `timestamp` remains the only stamp)
    # forward — stamp `generated: {by, at}` on writes, alongside `timestamp`
    generated_policy: str = "off"
    #: Actor recorded in ``generated.by``. SPEC §7 forms: ``<producer>/<version>``,
    #: ``human:<id>``, ``process:<id>``.
    generated_by: str = "apo/engine"


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


def spec_type_policy_override() -> str | None:
    """``APO_OKF_SPEC_TYPE=fill|mirror|off`` — escape hatch for ``type`` emission."""
    raw = os.environ.get("APO_OKF_SPEC_TYPE", "").strip().lower()
    if raw in {"fill", "mirror", "off"}:
        return raw
    return None


def generated_policy_override() -> str | None:
    """``APO_OKF_GENERATED=off|forward`` — escape hatch for provenance emission."""
    raw = os.environ.get("APO_OKF_GENERATED", "").strip().lower()
    if raw in {"off", "forward"}:
        return raw
    return None


def generated_updates(
    contract: OkfContract,
    scalars: dict[str, str],
    *,
    refreshed: bool = False,
    now: str | None = None,
) -> dict[str, dict[str, str]]:
    """Return the ``{generated: …}`` update for a write, or ``{}``.

    SPEC §13.1 supersedes the v0.1 ``timestamp`` with ``generated: {by, at}``.
    Emission is **forward-only**: a concept gets ``generated`` when it is first
    written under this policy, and its ``at`` is refreshed only when the same
    write also refreshed ``timestamp``. Existing notes are never backfilled —
    the engine does not know who generated content it did not write, and
    inventing an actor would poison the trust family it is meant to feed.

    The value is a real mapping; ``frontmatter.set_structured_fields`` renders
    it as a single-line flow mapping in Markdown frontmatter (the scalar setter
    would quote it into a string) and as a nested block in YAML notes.
    """
    policy = generated_policy_override() or contract.generated_policy
    if policy != "forward":
        return {}
    existing = (scalars.get("generated") or "").strip()
    if existing and not refreshed:
        return {}
    return {
        "generated": {
            "by": contract.generated_by or "apo/engine",
            "at": now or utc_now(),
        }
    }


def spec_type_updates(
    contract: OkfContract,
    scalars: dict[str, str],
    resolved_type: str,
) -> dict[str, str]:
    """Return the ``{type: …}`` update needed to make this concept OKF-conformant.

    SPEC §11 conformance clause 2 requires a non-empty ``type`` on every
    frontmatter block. Apo keeps ``okf_type`` as its native field and emits
    ``type`` alongside it; SPEC §11 forbids consumers rejecting a bundle for
    unknown additional keys, so carrying both is conformant.

    Under the default ``fill`` policy an existing non-empty ``type`` is left
    untouched — vaults that use ``type`` as a legacy taxonomy (Apo's
    ``legacy_type_map``) stay conformant on their own value rather than having
    it overwritten.
    """
    spec_field = (contract.spec_type_field or "").strip()
    policy = spec_type_policy_override() or contract.spec_type_policy
    if policy == "off" or not spec_field or spec_field == contract.type_field:
        return {}
    value = (resolved_type or "").strip()
    if not value:
        return {}
    existing = (scalars.get(spec_field) or "").strip()
    if policy == "mirror":
        return {} if existing == value else {spec_field: value}
    return {} if existing else {spec_field: value}


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

    spec_policy = str(data.get("spec_type_policy") or "fill").lower()
    if spec_policy not in {"fill", "mirror", "off"}:
        spec_policy = "fill"

    gen_policy = str(data.get("generated_policy") or "off").lower()
    if gen_policy not in {"off", "forward"}:
        gen_policy = "off"

    return OkfContract(
        path=path,
        okf_version=str(data.get("okf_version") or "0.1"),
        type_field=str(data.get("type_field") or "okf_type"),
        legacy_type_field=str(data.get("legacy_type_field") or "type"),
        spec_type_field=str(data.get("spec_type_field") or "type"),
        spec_type_policy=spec_policy,
        spec_required=[str(x) for x in (data.get("spec_required") or ["type"])],
        generated_policy=gen_policy,
        generated_by=str(data.get("generated_by") or "apo/engine"),
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
