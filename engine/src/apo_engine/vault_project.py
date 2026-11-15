"""Desk contract discovery for ``vault(request={action: "project"})``.

Owns vault-id scoping of the desk overlay, vault pointer expansion, typed
accessors over a vault row's parsed contracts, the drift signature, and the
watcher debounce. Rendering (desk markdown for host rule/skill surfaces and
the closed-vocabulary write/query grammars) lives in ``host_projection``.

Deterministic — no LLM.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

# Child of "apo" — see git_sync.py's logger comment; `maybe_reproject` is
# called from the watch loop with `verbose=True` in production.
logger = logging.getLogger("apo.vault_project")

# Watch / multi-caller debounce for auto-reproject.
_reproject_lock = threading.Lock()
_last_reproject_mono = 0.0
_last_poll_mono = 0.0
_last_desk_mtime: float | None = None
_last_contracts_sig: str | None = None
_MIN_REPROJECT_GAP_S = 2.0
# Minimum gap between *drift scans*. `_contracts_signature()` reloads the vault
# registry (a usage-contract YAML parse per vault) and stats every contract file,
# which costs tens of ms on a real desk. Every vault watcher thread polls this
# once per wake, so an ungated scan burns N_vaults x cost every second — the
# watcher sat at ~35% CPU permanently idle. Detection may lag by this gap;
# projection is advisory/return-only, so that is free.
_MIN_POLL_GAP_S = float(os.environ.get("APO_DESK_POLL_GAP_S") or 15.0)


def _abs_pointer(raw: str, vaults: dict[str, Any]) -> str:
    """Expand ``meta:path/to.md`` using merge vault roots; else return as-is."""
    text = (raw or "").strip()
    if not text or ":" not in text:
        return text
    # Avoid treating https: as vault
    if text.startswith(("http://", "https://", "/")):
        return text
    vault_id, _, rel = text.partition(":")
    row = vaults.get(vault_id)
    if not isinstance(row, dict):
        return text
    root = str(row.get("root") or "").rstrip("/")
    rel = rel.lstrip("/")
    if not root:
        return text
    if not rel.endswith(".md"):
        rel = f"{rel}.md"
    return f"{root}/{rel}"


def _pointer_vault_id(raw: str) -> str | None:
    """Return vault_id from ``vault_id:rel`` pointer, or None if not vault-prefixed."""
    text = (raw or "").strip()
    if not text or ":" not in text:
        return None
    if text.startswith(("http://", "https://", "/")):
        return None
    vault_id, _, _ = text.partition(":")
    return vault_id.strip() or None


def scope_desk_overlay(desk: dict[str, Any], vaults: dict[str, Any]) -> dict[str, Any]:
    """Trim desk ``role_notes`` / ``pointers`` to vaults present in the active registry.

    Active vaults come from the merge ``vaults`` dict (the discovery
    registry — APO_VAULTS_ROOT / APO_VAULT_PATHS, or legacy APO_VAULTS — filtered
    by ``vaults=``).
    Each binding carries a ``collection``; scoping is by vault name and assigned role.
    """
    out = dict(desk)
    active_names = set(vaults)
    active_roles = {
        str(row.get("role"))
        for row in vaults.values()
        if isinstance(row, dict) and row.get("role")
    }
    role_notes = desk.get("role_notes")
    if isinstance(role_notes, dict):
        out["role_notes"] = {
            role: note for role, note in role_notes.items() if role in active_roles
        }
    pointers = desk.get("pointers")
    if isinstance(pointers, dict):
        filtered: dict[str, Any] = {}
        for key, raw in pointers.items():
            vid = _pointer_vault_id(str(raw))
            if vid is None:
                # Absolute / URL / bare paths — keep (not cross-vault desk bleed).
                filtered[key] = raw
            elif vid in active_names:
                filtered[key] = raw
        out["pointers"] = filtered
    return out


def _contract_data(row: dict[str, Any], contract_id: str) -> dict[str, Any] | None:
    """Return a vault row's parsed ``<contract_id>`` body when present and ok.

    Requires ``vault_op("project", ...)`` to have merged with ``bodies=True`` —
    with ``bodies=False`` every contract entry is summary-only (``id``/``path``/
    ``source``/``ok``, no ``data``) and this always returns ``None``.
    """
    contracts = row.get("contracts") if isinstance(row.get("contracts"), dict) else {}
    entry = contracts.get(contract_id)
    if not isinstance(entry, dict) or not entry.get("ok", True):
        return None
    data = entry.get("data")
    return data if isinstance(data, dict) else None


def _usage_data(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return parsed usage-contract ``data`` when present and ok."""
    return _contract_data(row, "usage-contract")


def _usage_contribution(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return usage-contract ``contribution`` mapping when present."""
    data = _usage_data(row)
    if not data:
        return None
    contrib = data.get("contribution")
    return contrib if isinstance(contrib, dict) else None


def _usage_integrations(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return usage-contract ``integrations`` mapping when present."""
    data = _usage_data(row)
    if not data:
        return None
    integ = data.get("integrations")
    return integ if isinstance(integ, dict) else None


def _usage_pointers(row: dict[str, Any]) -> list[str]:
    """Return usage-contract root-level ``pointers`` list when present.

    Distinct from ``contribution.pointers`` / ``integrations.pointers`` —
    this is the vault's own policy pointer list (memory policy, write API,
    session-log conventions, ...), otherwise silently dropped from every
    projected desk body even though the contract author put it there.
    """
    data = _usage_data(row)
    if not data:
        return []
    raw = data.get("pointers")
    if not isinstance(raw, list):
        return []
    return [p.strip() for p in raw if isinstance(p, str) and p.strip()]


def _usage_write_habits(row: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Return ``write_habits`` (id, inline_text) pairs from usage-contract ``data``.

    Each entry may be a bare string id (resolved against ``_WRITE_HABIT_LINES``)
    or an inline object ``{id: ..., text: ...}`` carrying vault-authored guidance
    directly — for a vault dialect that has no matching dict entry, this is how
    it gets real projected text instead of the generic fallback line.
    """
    data = _usage_data(row)
    if not data:
        return []
    raw = data.get("write_habits")
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, str | None]] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append((item.strip(), None))
        elif isinstance(item, dict):
            hid = str(item.get("id") or "").strip()
            if not hid:
                continue
            text = str(item.get("text") or "").strip() or None
            out.append((hid, text))
    return out


def sources_hash(desk_mtime: float | None, contracts_sig: str) -> str:
    """Short fingerprint of the inputs a rendered desk depends on.

    Same inputs ``maybe_reproject`` watches (desk.yaml mtime + the contracts
    signature), hashed so a placed file can carry it and a fresh
    ``desk-project`` run can be compared against it.
    """
    raw = f"{desk_mtime if desk_mtime is not None else 'none'}|{contracts_sig}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def provenance(*, desk_mtime: float | None, contracts_sig: str) -> dict[str, str]:
    """``{generated_at, sources}`` for the merge IR — rendered as the header line."""
    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sources": sources_hash(desk_mtime, contracts_sig),
    }


def _desk_mtime() -> float | None:
    from . import vault_desk

    path = vault_desk.resolve_desk_path()
    if path is None or not path.is_file():
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _contracts_signature() -> str:
    """Cheap fingerprint of live contract files across the vault registry."""
    from . import vaults

    try:
        _default, bindings = vaults.load_bindings()
    except Exception:
        return ""
    parts: list[str] = []
    for name, b in sorted(bindings.items()):
        root = b.resolved().root
        cdir = root / "system" / "contracts"
        if not cdir.is_dir():
            continue
        try:
            for path in sorted(cdir.iterdir()):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in {".yaml", ".yml"}:
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                parts.append(f"{name}:{path.name}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            continue
    return "|".join(parts)


def is_contracts_rel(rel: str) -> bool:
    """True when a vault-relative path is under ``system/contracts/``."""
    r = (rel or "").replace("\\", "/").lstrip("/")
    return r == "system/contracts" or r.startswith("system/contracts/")


def project_live(vaults: list[str] | None = None, *, mode: str = "full") -> dict[str, Any]:
    """Build merge IR from the live registry/desk and project (CLI).

    ``vaults`` scopes to a named subset of the registry — see
    ``vault_op``'s docstring. ``mode`` selects Tier 1 (``"index"``) vs.
    Tier 2 (``"full"``, default) — see ``project``.
    """
    from . import ops

    return ops.vault_op("project", vaults=vaults, mode=mode)


def maybe_reproject(
    *,
    reason: str = "",
    force: bool = False,
    verbose: bool = False,
) -> dict[str, Any] | None:
    """Track desk.yaml / ``system/contracts/`` changes (projection is return-only).

    Debounced across vault watcher threads. Returns a change marker when desk or
    contracts drift; does not write host skill/rule files.
    """
    global _last_reproject_mono, _last_poll_mono, _last_desk_mtime, _last_contracts_sig

    with _reproject_lock:
        now = time.monotonic()
        # Drift scan is expensive; skip it entirely when polled again too soon.
        # Every vault watcher thread calls this once per wake, so this gate is
        # what keeps an idle desk off the CPU. `force` always scans.
        if not force and _last_poll_mono and (now - _last_poll_mono) < _MIN_POLL_GAP_S:
            return None
        _last_poll_mono = now
        desk_mt = _desk_mtime()
        sig = _contracts_signature()
        changed = force
        if not changed:
            if _last_contracts_sig is None:
                # First scan since startup: nothing has been projected in this
                # process, so report it (and seed below) regardless of desk mtime
                # — a desk without desk.yaml used to seed silently and never say
                # "render me" until a contract actually changed.
                changed = True
            elif desk_mt is not None and desk_mt != _last_desk_mtime:
                changed = True
            elif sig != _last_contracts_sig:
                changed = True
        if not changed:
            return None
        if not force and (now - _last_reproject_mono) < _MIN_REPROJECT_GAP_S:
            return None
        _last_reproject_mono = now
        _last_desk_mtime = desk_mt
        _last_contracts_sig = sig
        if verbose:
            logger.info(
                "[desk-project] desk/contracts changed (%s) — run desk-project to render",
                reason or "auto",
            )
        return {
            "ok": True,
            "changed": True,
            "reason": reason or ("force" if force else "auto"),
            "tip": (
                'Run `just desk-project` or vault(request={"action": "project"}) and place '
                "the rendered text"
            ),
        }
