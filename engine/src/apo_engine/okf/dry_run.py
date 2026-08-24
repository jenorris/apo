"""``okf_dry_run`` — simulate a proposed OKF contract against the live corpus.

Read-only: never writes anything, never touches the live contract file on
disk. Ported forward from the pre-package ``okf.py`` (v0.17.0) onto the
contract/frontmatter/stamp split introduced by the OKF spec-adoption work.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import yaml

from apo_engine.okf.contract import (
    OkfContract,
    PathRule,
    get_contract,
    load_contract,
    match_rule,
    path_glob_match,
)
from apo_engine.okf.frontmatter import _parse_scalars
from apo_engine.okf.stamp import _infer_okf_type

# Fields process_concept auto-derives on write (H1/title/stem/utc_now) — a
# violation on one of these self-heals the next time the note is written,
# unlike a custom required_field which has no derivation and needs a real
# value supplied. Surfaced as `auto_fillable` in okf_dry_run output rather
# than silently excluded, so the reviewer can judge either way.
_AUTO_FILLABLE_FIELDS = {"okf_type", "description", "timestamp", "title"}


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
