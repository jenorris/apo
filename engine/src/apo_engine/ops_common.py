"""Shared ops plumbing: OpsError, vault binding and path resolution, and the
small tip helpers every ops_* module uses."""

from __future__ import annotations

import posixpath
import sqlite3
from pathlib import Path
from typing import Any

from apo_engine import core, vaults
from apo_engine.note_format import (
    has_blocked_dot_segment,
    is_contract_schema_path,
    is_mutator_path,
    MUTATOR_SUFFIXES,
)
from apo_engine.path_ref import (
    merge_vault_arg,
    PathRefError,
    peel_path_ref,
    qualified_path as _qualified_path,
)


class OpsError(Exception):
    """Vault / path resolution failure with a stable error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _err(**kw: Any) -> dict[str, Any]:
    return {"ok": False, **kw}


def _sqlite_index_err(exc: BaseException) -> dict[str, Any] | None:
    """Map sqlite lock/busy failures to structured MCP payloads."""
    if isinstance(exc, core.IndexBusyError):
        return _err(error="index_busy", message=str(exc))
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        code = "index_busy" if "locked" in msg or "busy" in msg else "index_unavailable"
        return _err(error=code, message=str(exc))
    return None


def _binding(vault: str = "", *, write: bool = False) -> vaults.VaultBinding:
    """Resolve vault by name — must be in this process registry (write/read gate)."""
    default, bindings = vaults.load_bindings()
    key = (vault or "").strip() or default
    if key not in bindings:
        raise OpsError("bad_vault", f"unknown vault {key!r}; available: {sorted(bindings)}")
    binding = bindings[key]
    if write and binding.read_only:
        raise OpsError(
            "read_only_vault",
            f"vault {key!r} is mounted read-only; searchable but not writable",
        )
    return binding


def _load_bindings():
    """Thin indirection so callers can name a local param ``vaults`` without
    shadowing the ``apo_engine.vaults`` module import."""
    return vaults.load_bindings()


def _resolve_binding_and_rel(
    path: str, vault: str = "", *, write: bool = False
) -> tuple[vaults.VaultBinding, str]:
    """Peel optional ``vault_id:rel`` prefix; gate vault_id to process registry.

    Mutating and path-bearing ops use this so writes cannot target vaults outside
    the MCP process bindings. ``write=True`` additionally rejects a vault
    mounted read-only, same gate as ``_binding(..., write=True)``.
    """
    default, bindings = vaults.load_bindings()
    known = set(bindings)
    try:
        pref, rel = peel_path_ref(path, known=known)
        key = merge_vault_arg(pref, vault, default=default)
    except PathRefError as e:
        raise OpsError(e.code, e.message) from e
    if key not in bindings:
        raise OpsError("bad_vault", f"unknown vault {key!r}; available: {sorted(bindings)}")
    binding = bindings[key]
    if write and binding.read_only:
        raise OpsError(
            "read_only_vault",
            f"vault {key!r} is mounted read-only; searchable but not writable",
        )
    return binding, rel


def _stamp_qualified(out: dict[str, Any], *, vault: str, path: str) -> dict[str, Any]:
    """Add ``qualified_path`` for agent copy-paste (non-breaking additive field)."""
    if out.get("ok") and vault and path:
        out["qualified_path"] = _qualified_path(vault, path)
    return out


def _safe_resolve(root: Path, relative_path: str) -> Path:
    full = (root / relative_path).resolve()
    rel = full.relative_to(root)  # raises ValueError on traversal
    # Checked on the resolved target: a symlink like ``leak.md -> .env`` is
    # named like a note but must not read or write the protected file.
    if has_blocked_dot_segment(rel.as_posix()):
        raise ValueError("path is not a note")
    return full


def _require_note_path(
    path: str,
    *,
    root: Path,
    full: Path | None = None,
) -> None:
    """Reject mutator I/O on protected or non-note canonical targets.

    ``.json`` catalogs pass here for raw writes and object patch targets.
    """
    try:
        resolved_root = root.resolve()
        lexical = posixpath.normpath(path.replace("\\", "/"))
        lexical_full = _safe_resolve(resolved_root, lexical)
        resolved_full = (full or lexical_full).resolve()
        canonical = {
            lexical_full.relative_to(resolved_root).as_posix(),
            resolved_full.relative_to(resolved_root).as_posix(),
        }
    except ValueError as exc:
        raise OpsError("bad_path", str(exc)) from exc
    if any(has_blocked_dot_segment(candidate) for candidate in canonical):
        raise OpsError("bad_path", "path is not a note")
    if any(is_contract_schema_path(candidate) for candidate in canonical):
        raise OpsError(
            "protected_control_plane",
            "contract schema files are host-controlled; use filesystem or CLI administration",
        )
    if all(is_mutator_path(candidate) for candidate in canonical):
        return
    allowed = ", ".join(sorted(MUTATOR_SUFFIXES))
    suffix = Path(lexical).suffix or "(none)"
    raise OpsError(
        "unsupported_format",
        f"Apo mutators accept note paths only ({allowed}); "
        f"got suffix {suffix!r} — use host filesystem for scripts and other files",
    )


def _mtime(full: Path) -> float:
    return full.stat().st_mtime


def _io_error(exc: OSError, full: Path, path: str, **kw: Any) -> dict[str, Any]:
    """Map a write-path ``OSError`` to an ``io_error`` payload.

    Carries the OS reason and the vault-relative ``path`` only: the absolute
    target (and its sibling temp file) never reaches the caller.
    """
    reason = str(exc.strerror or type(exc).__name__)
    for absolute in (str(full), str(full.parent)):
        reason = reason.replace(absolute, path)
    fields = kw or {"path": path}
    return _err(**fields, error="io_error", message=f"cannot write {path}: {reason}")


def _top_level_dirs(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))


_FOLDER_TIP = (
    "pass folder= when the PARA bucket is known — unscoped search is slower and noisier"
)


def _attach_tip(out: dict[str, Any], tip: str) -> dict[str, Any]:
    """Soft agent habit hint (not a failure). Does not overwrite ``warning``."""
    if not out.get("ok") or not tip:
        return out
    existing = out.get("tip")
    out["tip"] = f"{existing}; {tip}" if existing else tip
    return out


def _attach_folder_tip(
    out: dict[str, Any],
    folder_clean: str,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    if folder_clean:
        return out
    tip = _FOLDER_TIP
    if root is not None:
        dirs = _top_level_dirs(root)
        if dirs:
            shown = "|".join(dirs[:8])
            tip = (
                f"pass folder= ({shown}) when the PARA bucket is known — "
                "unscoped search is slower and noisier"
            )
    return _attach_tip(out, tip)


def _attach_flaws(out: dict[str, Any], flaws: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge corpus findings onto a successful response. Does not touch tip/warning."""
    if not out.get("ok") or not flaws:
        return out
    existing = out.get("flaws")
    if isinstance(existing, list) and existing:
        out["flaws"] = list(existing) + list(flaws)
    else:
        out["flaws"] = list(flaws)
    return out


def _missing_folder_warning(root: Path, folder_clean: str, vault_name: str) -> str | None:
    """Non-fatal warning when a folder= scope does not exist on disk.

    A typo'd folder (or the right folder in the wrong vault) otherwise returns
    a silent empty result set that agents read as \"no knowledge here\".
    """
    if not folder_clean:
        return None
    try:
        full = _safe_resolve(root, folder_clean)
    except ValueError:
        return None  # traversal is rejected by callers before this point
    if full.is_dir():
        return None
    return (
        f"folder {folder_clean!r} does not exist in vault {vault_name!r} — "
        "results are empty by construction; check spelling or pass vault=. "
        f"Top-level dirs: {_top_level_dirs(root)}"
    )
