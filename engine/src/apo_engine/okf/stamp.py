"""OKF stamp (write path) and validate (read path).

``process_concept`` is what every Apo write goes through: it resolves the path
rule, stamps missing core fields, and validates. ``validate_concept`` is the
read-only counterpart used by lint / CLI, with a producer profile and a
spec-exact consumer profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apo_engine.okf.contract import (
    OkfContract,
    PathRule,
    enforcement_override,
    generated_updates,
    get_contract,
    match_rule,
    spec_type_updates,
    utc_now,
)
from apo_engine.okf.frontmatter import (
    first_h1 as _first_h1,
    has_frontmatter as _has_frontmatter,
    parse_scalars as _parse_scalars,
    set_fields as _set_fields,
    set_structured_fields,
)

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
        if self.violations:
            out["violations"] = self.violations
        return out


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

    # OKF interchange: emit the spec's ``type`` next to Apo's ``okf_type``.
    for key, val in spec_type_updates(contract, scalars, inferred).items():
        updates[key] = val
        stamped.append(key)

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

    # v0.2 provenance, forward-only. Refreshes `generated.at` only when this
    # same write refreshed `timestamp`, so the two never disagree. Nested
    # value, so it is applied separately from the scalar updates below.
    structured = generated_updates(contract, scalars, refreshed="timestamp" in stamped)
    stamped.extend(structured)

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
    new_content = set_structured_fields(new_content, structured, rel_path=rel)
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
        violations=violations if enf == "hard" else [],
        okf_type=okf_type,
        enforcement=enf,
        ok=True,
    )


@dataclass
class ValidationReport:
    """Read-only conformance check — no stamping, no mutation."""

    rel_path: str
    profile: str = "apo"
    okf_type: str | None = None
    enforcement: str = "off"
    violations: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def validate_concept(
    *,
    vault_root: Path,
    rel_path: str,
    content: str,
    profile: str = "apo",
) -> ValidationReport:
    """Validate an existing note without mutating it.

    ``profile="okf"`` checks **exactly** SPEC §11 conformance: parseable
    frontmatter on every non-reserved ``.md``, a non-empty ``type`` in it, and
    reserved filenames carrying no concept frontmatter. It deliberately does
    **not** require Apo's ``description`` / ``timestamp`` — §11 forbids a
    consumer rejecting a bundle for missing optional fields.

    ``profile="apo"`` is the stricter producer profile: the contract's
    ``core_required`` plus any ``path_rules[].required_fields``.
    """
    rel = rel_path.replace("\\", "/").lstrip("/")
    prof = (profile or "apo").lower()
    report = ValidationReport(rel_path=rel, profile=prof)

    contract = get_contract(vault_root)
    if contract is None:
        return report

    rule = match_rule(contract, rel)
    enf = rule.enforcement if rule else contract.default_enforcement
    report.enforcement = enf

    has_fm = _has_frontmatter(content, rel)
    scalars = _parse_scalars(content, rel)
    spec_field = (contract.spec_type_field or "type").strip()

    if enf == "reserved":
        if has_fm:
            report.violations.append(
                {"field": "frontmatter", "expected": "absent", "path": rel}
            )
        return report

    if prof == "okf":
        # SPEC §11.1/§11.2 scope themselves to *non-reserved* files; reserved
        # filenames are governed by §11.3 instead. The bundle-root index.md is
        # reserved but may carry frontmatter (it declares okf_version), so it
        # must not be asked for a concept `type`.
        if Path(rel).name in contract.reserved_filenames:
            if rel == "index.md":
                if has_fm and not (scalars.get("okf_version") or "").strip():
                    report.warnings.append(
                        f"{rel}: bundle root index.md should declare okf_version"
                    )
            elif has_fm:
                report.violations.append(
                    {"field": "frontmatter", "expected": "absent", "path": rel}
                )
            return report

        # SPEC §11.1 — every non-reserved .md carries parseable frontmatter.
        if not has_fm:
            report.violations.append(
                {"field": "frontmatter", "expected": "present", "path": rel}
            )
            return report
        # SPEC §11.2 — that frontmatter has a non-empty ``type``.
        if not (scalars.get(spec_field) or "").strip():
            native = (scalars.get(contract.type_field) or "").strip()
            hint = f"non-empty; okf_type={native}" if native else "non-empty"
            report.violations.append({"field": spec_field, "expected": hint, "path": rel})
        report.okf_type = (
            scalars.get(contract.type_field) or scalars.get(spec_field) or ""
        ).strip() or None
        return report

    # --- producer profile -----------------------------------------------------
    if enf in {"off", "exempt"}:
        report.okf_type = (scalars.get(contract.type_field) or "").strip() or None
        return report

    if not has_fm:
        report.violations.append({"field": "frontmatter", "expected": "present", "path": rel})
        return report

    required = list(contract.core_required)
    if rule and rule.required_fields:
        for f in rule.required_fields:
            if f not in required:
                required.append(f)

    for f in required:
        if f == "timestamp":
            if not any(
                (scalars.get(k) or "").strip()
                for k in ("timestamp", "updated", "ingested_at", "date")
            ):
                report.violations.append(
                    {"field": "timestamp", "expected": "ISO-8601", "path": rel}
                )
            continue
        if not (scalars.get(f) or "").strip():
            report.violations.append({"field": f, "expected": "non-empty", "path": rel})

    report.okf_type = (scalars.get(contract.type_field) or "").strip() or None
    return report
