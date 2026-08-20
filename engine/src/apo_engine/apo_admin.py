"""Apo admin meta-tool — list / describe / invoke engine ops.

Replaces top-level admin MCP tools. Destructive capabilities require
``confirm=true`` on ``invoke``.
"""

from __future__ import annotations

from typing import Any, Callable

AdminHandler = Callable[..., dict[str, Any]]

_ADMIN_CATALOG: dict[str, dict[str, Any]] = {
    "reload_config": {
        "description": (
            "Reload the vault discovery registry (APO_COLLECTION_ROOT / APO_VAULT_PATHS / legacy APO_VAULTS) + runtime JSON overrides into the MCP process and nudge "
            "the watcher (wake-registry) to hot-add newly registered vaults. Removals "
            "or root/index path changes still require a watcher restart."
        ),
        "read_only": False,
        "destructive": False,
        "confirm_policy": None,
        "parameters": {},
    },
    "memory_status": {
        "description": (
            "Vault roots, index health, deferred queues, watcher state — diagnose "
            "before retrying failures."
        ),
        "read_only": True,
        "destructive": False,
        "confirm_policy": None,
        "parameters": {},
    },
    "reindex": {
        "description": (
            "Index maintenance: mode=flush wakes deferred queue (empty vault = all vaults); "
            "mode=rebuild signals full rebuild (single vault). force=true re-embeds all."
        ),
        "read_only": False,
        "destructive": False,
        "confirm_policy": "force=true",
        "parameters": {
            "mode": "flush | rebuild (default rebuild)",
            "force": "bool — rebuild only; re-embed all chunks (default false)",
            "vault": "vault name (empty = all vaults for flush only)",
        },
    },
    "delete_note": {
        "description": (
            "Irreversible delete + index purge. Prefer patch_note place op to archives/. "
            "Always requires confirm=true."
        ),
        "read_only": False,
        "destructive": True,
        "confirm_policy": "always",
        "parameters": {
            "path": "vault-relative note path (required)",
            "vault": "vault name (default registry default)",
        },
    },
    "git_sync": {
        "description": (
            "Git contract sync: status, commit+push (run), ff-only pull, rebase "
            "local commits onto the remote tip when pull can't fast-forward, or "
            "clear_block. Opt-in via sync.enabled."
        ),
        "read_only": False,
        "destructive": False,
        "confirm_policy": "action=run|pull|rebase",
        "parameters": {
            "action": "status | run | pull | rebase | clear_block (default status)",
            "message": "commit subject for action=run (optional)",
            "vault": "vault name (default registry default)",
        },
    },
    "list_refs": {
        "description": (
            "List reachable git refs (branches / jj bookmarks, optional tags) at the "
            "vault registry root for filter_notes/read_note ref= discovery. Read-only."
        ),
        "read_only": True,
        "destructive": False,
        "confirm_policy": None,
        "parameters": {
            "kind": "heads | tags | all (default heads)",
            "vault": "vault name (default registry default)",
        },
    },
}

ADMIN_NAMES = frozenset(_ADMIN_CATALOG)


def _err(**kw: Any) -> dict[str, Any]:
    return {"ok": False, **kw}


def _capability_summary(name: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": meta["description"],
        "read_only": meta["read_only"],
        "destructive": meta.get("destructive", False),
        "confirm_policy": meta.get("confirm_policy"),
    }


def _needs_confirm(name: str, parameters: dict[str, Any]) -> bool:
    if name == "delete_note":
        return True
    if name == "reindex" and bool(parameters.get("force")):
        return True
    if name == "git_sync":
        action = str(parameters.get("action") or "status").strip().lower()
        return action in ("run", "pull", "rebase")
    return False


def admin_list() -> dict[str, Any]:
    return {
        "ok": True,
        "action": "list",
        "capabilities": [
            _capability_summary(name, meta)
            for name, meta in sorted(_ADMIN_CATALOG.items())
        ],
        "usage": (
            "apo_admin(action=describe, name=…) for parameter schemas; "
            "apo_admin(action=invoke, name=…, parameters={…}, confirm=true) when "
            "confirm_policy applies."
        ),
    }


def admin_describe(name: str) -> dict[str, Any]:
    key = (name or "").strip()
    meta = _ADMIN_CATALOG.get(key)
    if meta is None:
        return _err(
            error="bad_name",
            message=f"unknown admin capability {key!r}; use action=list",
            available=sorted(_ADMIN_CATALOG),
        )
    out = _capability_summary(key, meta)
    out.update(
        {
            "ok": True,
            "action": "describe",
            "parameters": meta.get("parameters") or {},
        }
    )
    if meta.get("confirm_policy"):
        out["confirm"] = (
            f"Pass confirm=true on invoke when confirm_policy is {meta['confirm_policy']!r}."
        )
    return out


def admin_invoke(
    name: str,
    *,
    parameters: dict[str, Any] | None = None,
    confirm: bool = False,
    vault: str = "",
    handlers: dict[str, AdminHandler],
) -> dict[str, Any]:
    key = (name or "").strip()
    if key not in ADMIN_NAMES:
        return _err(
            error="bad_name",
            message=f"unknown admin capability {key!r}; use action=list",
            available=sorted(_ADMIN_CATALOG),
        )
    handler = handlers.get(key)
    if handler is None:
        return _err(error="internal", message=f"no handler registered for {key!r}")

    params = dict(parameters or {})
    if _needs_confirm(key, params) and not confirm:
        policy = _ADMIN_CATALOG[key].get("confirm_policy") or "always"
        return _err(
            error="confirm_required",
            message=(
                f"Pass confirm=true to invoke {key!r} "
                f"(confirm_policy={policy!r})."
            ),
            capability=key,
            confirm_policy=policy,
        )

    try:
        result = handler(params, vault=vault)
    except TypeError as e:
        return _err(error="bad_parameters", message=str(e), capability=key)
    if isinstance(result, dict):
        out = dict(result)
        out.setdefault("ok", True)
        out["admin_capability"] = key
        return out
    return {"ok": True, "admin_capability": key, "result": result}
