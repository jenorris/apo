"""Usage-contract shape checks — host-neutral vault usage IR.

The engine does not interpret usage-contract fields for search/write. It reads
them in two places: registry discovery (``vaults.read_usage_vault_id`` — the
tool-facing vault name) and desk projection (``vault_project`` — pointers,
``write_habits``, ``token_budget``, contribution / integrations). A malformed
entry there used to be dropped silently, unlike every other contract id.

Unknown top-level keys are deliberately *not* flagged: the file is documented
as harness-extensible (template: ``docs/contracts/usage-contract.schema.yaml``).
"""

from __future__ import annotations

import re
from typing import Any

from apo_engine import vault_contracts as vc

# ``vault_id:relative/path`` — the same shape ``vault_project._abs_pointer`` expands.
_POINTER_VAULT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def pointer_finding(value: Any, field: str) -> dict[str, str] | None:
    """``contract.invalid_value`` unless ``value`` is a qualified pointer.

    Accepted forms match what projection resolves: ``vault_id:relative/path``,
    an absolute path, or an ``http(s)://`` URL. A bare relative path has no
    vault to resolve against and renders as dead text.
    """
    if not isinstance(value, str) or not value.strip():
        return vc.finding("contract.invalid_shape", field, f"{field} must be a non-empty string")
    text = value.strip()
    if text.startswith(("http://", "https://", "/")):
        return None
    vault_id, sep, rel = text.partition(":")
    if sep and _POINTER_VAULT_RE.match(vault_id) and rel.strip("/").strip():
        return None
    return vc.finding(
        "contract.invalid_value",
        field,
        f"{field} {value!r} is not a qualified pointer (vault_id:relative/path, absolute path, or URL)",
    )


def pointer_list_findings(value: Any, field: str) -> list[dict[str, str]]:
    """Per-entry pointer findings; ``[]`` when absent."""
    if value is None:
        return []
    if not isinstance(value, list):
        return [vc.finding("contract.invalid_shape", field, f"{field} must be a list of pointers")]
    out: list[dict[str, str]] = []
    for i, item in enumerate(value):
        f = pointer_finding(item, f"{field}[{i}]")
        if f:
            out.append(f)
    return out


def check_contract(data: dict[str, Any]) -> list[dict[str, str]]:
    """Shape findings for a parsed usage contract (advisory, never blocks)."""
    findings: list[dict[str, str]] = []

    # Optional (a vault_id-only contract is enough for discovery) — but when set
    # it must be a version scalar, like every other contract's *_version key.
    version = data.get("usage_contract_version")
    if version is not None and (isinstance(version, bool) or not isinstance(version, (str, int, float))):
        findings.append(
            vc.finding(
                "contract.invalid_shape",
                "usage_contract_version",
                "usage_contract_version must be a version string (e.g. '0.1')",
            )
        )

    vault_id = data.get("vault_id")
    if not isinstance(vault_id, str) or not vault_id.strip():
        findings.append(
            vc.finding(
                "contract.invalid_shape",
                "vault_id",
                "vault_id must be a non-empty string (the registry / tool-facing vault name)",
            )
        )

    findings.extend(pointer_list_findings(data.get("pointers"), "pointers"))

    contribution = data.get("contribution")
    f = vc.mapping_finding(contribution, "contribution")
    if f:
        findings.append(f)
    elif isinstance(contribution, dict):
        findings.extend(pointer_list_findings(contribution.get("pointers"), "contribution.pointers"))

    integrations = data.get("integrations")
    f = vc.mapping_finding(integrations, "integrations")
    if f:
        findings.append(f)
    elif isinstance(integrations, dict):
        findings.extend(pointer_list_findings(integrations.get("pointers"), "integrations.pointers"))

    budget = data.get("token_budget")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int)):
        findings.append(
            vc.finding("contract.invalid_value", "token_budget", f"token_budget {budget!r} must be an integer")
        )

    habits = data.get("write_habits")
    if habits is not None:
        if not isinstance(habits, list):
            findings.append(
                vc.finding("contract.invalid_shape", "write_habits", "write_habits must be a list")
            )
        else:
            for i, item in enumerate(habits):
                ok = (isinstance(item, str) and item.strip()) or (
                    isinstance(item, dict) and str(item.get("id") or "").strip()
                )
                if not ok:
                    findings.append(
                        vc.finding(
                            "contract.invalid_shape",
                            f"write_habits[{i}]",
                            "write_habits entries must be an id string or {id, text} mapping",
                        )
                    )
    return findings
