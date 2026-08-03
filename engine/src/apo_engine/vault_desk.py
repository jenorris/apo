"""Desk overlay for ``vault(action=merge)``.

Host-level (not vault content): ``~/.apo/desk.yaml`` or ``APO_DESK_CONFIG``.
Holds cross-vault policy (dual-write, citations, roles). Per-vault contracts
stay under each vault's ``system/contracts/`` and are never cross-pollinated.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_DESK_PATH = Path.home() / ".apo" / "desk.yaml"

# Minimal defaults when no desk.yaml exists — still deterministic.
_DEFAULT_DESK: dict[str, Any] = {
    "desk_version": "0.1",
    "cross_pollinate_contracts": False,
    "citations": "absolute_markdown",
    "dual_write": {
        "session_vault": "sessions",
        "session_path_template": "inbox/daily/{date}.md",
        "session_heading": "Session log",
    },
    "vault_roles": {},
    "habits": {
        "end_of_turn_gate": True,
        "new_durable_facts": True,
        "prefer_append_patch": True,
        "filter_okf_type": True,
    },
    "pointers": {},
    "role_notes": {},
    "workspace": "",
}


def resolve_desk_path(explicit: str | None = None) -> Path | None:
    if explicit is None:
        explicit = os.environ.get("APO_DESK_CONFIG", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    return DEFAULT_DESK_PATH if DEFAULT_DESK_PATH.is_file() else None


def load_desk(explicit: str | None = None) -> dict[str, Any]:
    """Return desk overlay mapping. Missing file → defaults (source=defaults)."""
    path = resolve_desk_path(explicit)
    if path is None:
        out = dict(_DEFAULT_DESK)
        out["_source"] = "defaults"
        out["_path"] = None
        return out
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        out = dict(_DEFAULT_DESK)
        out["_source"] = "error"
        out["_path"] = str(path)
        out["_error"] = str(e)
        return out
    if not isinstance(data, dict):
        out = dict(_DEFAULT_DESK)
        out["_source"] = "error"
        out["_path"] = str(path)
        out["_error"] = "desk root must be a mapping"
        return out
    # Shallow-merge defaults under user keys (user wins).
    merged = dict(_DEFAULT_DESK)
    for key, val in data.items():
        if key.startswith("_"):
            continue
        if (
            key in ("dual_write", "vault_roles", "habits", "pointers", "role_notes")
            and isinstance(val, dict)
            and isinstance(merged.get(key), dict)
        ):
            nested = dict(merged[key])
            nested.update(val)
            merged[key] = nested
        else:
            merged[key] = val
    merged["_source"] = "file"
    merged["_path"] = str(path)
    return merged


def public_desk(desk: dict[str, Any]) -> dict[str, Any]:
    """Strip internal keys for tool payloads."""
    return {k: v for k, v in desk.items() if not str(k).startswith("_")}
