"""Local-web contract loader — per-vault ``just serve`` config.

Active when ``system/contracts/local-web-contract.schema.yaml`` (or legacy
``system/config/local-web-contract.schema.yaml``) exists under the vault root.
Read-only: the local HTML browser process reads this directly; the engine's
only consumer is desk projection (``vault_project.format_local_web_line``),
which surfaces bind/port/mode as a one-liner when present.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from apo_engine import vault_contracts as vc

LOCAL_WEB_CONTRACT_CANDIDATES = (
    Path("system") / "contracts" / "local-web-contract.schema.yaml",
    Path("system") / "config" / "local-web-contract.schema.yaml",
)
LOCAL_WEB_CONTRACT_REL = LOCAL_WEB_CONTRACT_CANDIDATES[0]
MODES = ("adaptive", "fixed")
KNOWN_TOP_KEYS = frozenset(
    {
        "local_web_version",
        "bind",
        "port",
        "mode",
        "browse_roots",
        "exclude_globs",
        "audit",
        "features",
        "render",
    }
)
KNOWN_AUDIT_KEYS = frozenset({"enabled", "default_root", "source_label"})
KNOWN_FEATURE_KEYS = frozenset({"search", "filter", "backlinks", "mermaid", "math"})
KNOWN_RENDER_KEYS = frozenset(
    {
        "engine",
        "default_layout",
        "stylesheet",
        "template",
        "header",
        "strip_frontmatter",
        "resolve_wikilinks",
    }
)


def resolve_local_web_contract_path(vault_root: Path, explicit: str | None = None) -> Path | None:
    if explicit is None:
        explicit = os.environ.get("APO_LOCAL_WEB_CONTRACT", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    for rel in LOCAL_WEB_CONTRACT_CANDIDATES:
        candidate = vault_root / rel
        if candidate.is_file():
            return candidate
    return None


def load_local_web_contract(vault_root: Path, explicit: str | None = None) -> dict[str, Any] | None:
    """Parse local-web-contract YAML if present. Returns None when missing/unreadable."""
    path = resolve_local_web_contract_path(vault_root, explicit)
    if path is None:
        return None
    return vc.load_yaml_cached(path)


def _check_contract(data: dict[str, Any]) -> list[dict[str, str]]:
    """Shape findings for local browser and desk-projection fields."""
    findings = vc.unknown_key_findings(data, KNOWN_TOP_KEYS)
    for key in ("local_web_version", "bind"):
        value = data.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            findings.append(
                vc.finding(
                    "contract.invalid_shape",
                    key,
                    f"{key} must be a non-empty string",
                )
            )
    f = vc.enum_finding(data.get("mode"), MODES, "mode")
    if f:
        findings.append(f)
    port = data.get("port")
    if port is not None and (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
    ):
        findings.append(
            vc.finding(
                "contract.invalid_value",
                "port",
                f"port {port!r} must be an integer from 1 to 65535",
            )
        )
    findings.extend(vc.str_list_findings(data.get("browse_roots"), "browse_roots"))
    findings.extend(vc.str_list_findings(data.get("exclude_globs"), "exclude_globs"))
    for key, known in (
        ("audit", KNOWN_AUDIT_KEYS),
        ("features", KNOWN_FEATURE_KEYS),
        ("render", KNOWN_RENDER_KEYS),
    ):
        value = data.get(key)
        f = vc.mapping_finding(value, key)
        if f:
            findings.append(f)
        elif isinstance(value, dict):
            findings.extend(vc.unknown_key_findings(value, known, prefix=key))
            for child, child_value in value.items():
                field = f"{key}.{child}"
                if key == "features" or child in {"enabled", "strip_frontmatter"}:
                    if not isinstance(child_value, bool):
                        findings.append(
                            vc.finding(
                                "contract.invalid_shape",
                                field,
                                f"{field} must be a boolean",
                            )
                        )
                elif child in known and (
                    not isinstance(child_value, str) or not child_value.strip()
                ):
                    findings.append(
                        vc.finding(
                            "contract.invalid_shape",
                            field,
                            f"{field} must be a non-empty string",
                        )
                    )
    return findings


def parse_contract(data: dict[str, Any]) -> vc.ContractParse:
    """Normalize containers consumed by the local browser schema."""
    normalized = dict(data)
    for key in ("audit", "features", "render"):
        normalized[key] = data.get(key) if isinstance(data.get(key), dict) else {}
    for key in ("browse_roots", "exclude_globs"):
        normalized[key] = data.get(key) if isinstance(data.get(key), list) else []
    return vc.ContractParse(normalized, tuple(_check_contract(data)))


def check_contract(data: dict[str, Any]) -> list[dict[str, str]]:
    return list(parse_contract(data).findings)
