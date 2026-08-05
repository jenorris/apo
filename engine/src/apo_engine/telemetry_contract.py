"""Telemetry contract loader — vault-defined privacy for tool-use metrics."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

TELEMETRY_CONTRACT_CANDIDATES = (
    Path("system") / "contracts" / "telemetry-contract.schema.yaml",
    Path("system") / "config" / "telemetry-contract.schema.yaml",
)

PathMode = str  # none | hash_only | vault_relative | absolute


@dataclass(frozen=True)
class TelemetryPolicy:
    enabled: bool = True
    paths_mode: PathMode = "none"
    record_heading: bool = False
    record_chunk_hash: bool = False
    record_conversation_id: bool = True
    expose_paths: bool = False
    vault_id: str = ""

    def allows_note_path(self) -> bool:
        return self.paths_mode in ("hash_only", "vault_relative", "absolute")

    def store_path_hash(self) -> bool:
        return self.paths_mode in ("hash_only", "vault_relative", "absolute")

    def store_vault_relative_path(self) -> bool:
        return self.paths_mode in ("vault_relative", "absolute")


def _parse_paths_mode(raw: Any) -> PathMode:
    mode = str(raw or "none").strip().lower()
    if mode in ("none", "hash_only", "vault_relative", "absolute"):
        return mode
    return "none"


def resolve_telemetry_contract_path(
    vault_root: Path, explicit: str | None = None
) -> Path | None:
    if explicit is None:
        explicit = os.environ.get("APO_TELEMETRY_CONTRACT", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    for rel in TELEMETRY_CONTRACT_CANDIDATES:
        candidate = vault_root / rel
        if candidate.is_file():
            return candidate
    return None


def load_telemetry_contract(
    vault_root: Path, explicit: str | None = None
) -> dict[str, Any] | None:
    path = resolve_telemetry_contract_path(vault_root, explicit)
    if path is None:
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def policy_from_contract(data: dict[str, Any] | None) -> TelemetryPolicy:
    if not data:
        return TelemetryPolicy()
    if data.get("enabled") is False:
        return TelemetryPolicy(enabled=False)
    privacy = data.get("privacy") if isinstance(data.get("privacy"), dict) else {}
    allow = privacy.get("allow") if isinstance(privacy.get("allow"), dict) else {}
    agent = (
        data.get("agent_access")
        if isinstance(data.get("agent_access"), dict)
        else {}
    )
    return TelemetryPolicy(
        enabled=True,
        paths_mode=_parse_paths_mode(allow.get("paths")),
        record_heading=bool(allow.get("headings")),
        record_chunk_hash=bool(allow.get("chunk_hash")),
        record_conversation_id="conversation_id"
        in (allow.get("dimensions") or ["conversation_id"]),
        expose_paths=bool(agent.get("expose_paths")),
        vault_id=str(data.get("vault_id") or "").strip(),
    )


def policy_for_vault(vault_root: Path | None) -> TelemetryPolicy:
    if vault_root is None:
        return TelemetryPolicy()
    data = load_telemetry_contract(vault_root)
    return policy_from_contract(data)


def normalize_note_path(raw: str) -> str:
    return raw.replace("\\", "/").strip().lstrip("/")


def path_hash(note_path: str) -> str:
    normalized = normalize_note_path(note_path)
    if not normalized:
        return ""
    return hashlib.blake2b(normalized.encode("utf-8"), digest_size=16).hexdigest()


def extract_note_context(
    tool: str,
    arguments: dict[str, Any] | None,
    policy: TelemetryPolicy,
) -> dict[str, str]:
    """Privacy-filtered path/heading/chunk fields for metrics ingest."""
    out: dict[str, str] = {}
    if not policy.allows_note_path():
        return out
    args = arguments if isinstance(arguments, dict) else {}
    raw_path = str(args.get("path") or "").strip()
    if raw_path and policy.store_vault_relative_path():
        out["note_path"] = normalize_note_path(raw_path)
    if raw_path and policy.store_path_hash():
        out["path_hash"] = path_hash(raw_path)
    if policy.record_heading:
        heading = str(args.get("heading") or "").strip()
        if heading:
            out["heading"] = heading[:500]
    if policy.record_chunk_hash:
        ch = str(args.get("chunk_hash") or "").strip()
        if ch:
            out["chunk_hash"] = ch[:128]
    return out


def conversation_id_from_active_session() -> str | None:
    p = active_session_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    raw = str(data.get("conversation_id") or data.get("session_id") or "").strip()
    return raw or None


def conversation_id_from_env() -> str | None:
    raw = os.environ.get("APO_CONVERSATION_ID", "").strip()
    if raw:
        return raw
    return conversation_id_from_active_session()


def active_session_path() -> Path:
    raw = os.environ.get("APO_DEFERRED_DIR", "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / ".apo"
    return base / "active-session.json"
