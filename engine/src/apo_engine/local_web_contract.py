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

import yaml

LOCAL_WEB_CONTRACT_CANDIDATES = (
    Path("system") / "contracts" / "local-web-contract.schema.yaml",
    Path("system") / "config" / "local-web-contract.schema.yaml",
)
LOCAL_WEB_CONTRACT_REL = LOCAL_WEB_CONTRACT_CANDIDATES[0]


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
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None
