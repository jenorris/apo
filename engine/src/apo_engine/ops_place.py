"""Ops place surface: place_note, move_note, send_note and delete_note. Each
holds ``note_write_lock`` around its check-and-write section."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from apo_engine import config, deferred as index_deferred, okf as apo_okf
from apo_engine.atomic_io import atomic_write_text, note_write_lock
from apo_engine.markdown_patch import apply_patch
from apo_engine.validation_hints import flatten_patch_failure_error
from apo_engine.ops_common import (
    _attach_flaws,
    _err,
    _io_error,
    _mtime,
    _require_note_path,
    _resolve_binding_and_rel,
    _safe_resolve,
    _stamp_qualified,
    _top_level_dirs,
    OpsError,
)
from apo_engine.ops_admin import _attach_watcher_tip
from apo_engine.ops_guard import (
    _check_mtime,
    _enqueue_index,
    _enqueue_purge,
    _finalize_write,
    _prepare_write_content,
    _reject_if_ref,
)


def _send_allow_roots() -> list[Path]:
    raw = (config.SEND_ALLOW_ROOTS or "").strip()
    if not raw:
        return []
    roots: list[Path] = []
    for part in raw.split(":"):
        p = part.strip()
        if not p:
            continue
        roots.append(Path(p).expanduser().resolve())
    return roots


def _resolve_send_src(src: str, vault_root: Path) -> Path:
    """Resolve a host-path markdown source for send_note.

    Raises OpsError with a stable code on failure.
    """
    raw = (src or "").strip()
    if not raw:
        raise OpsError("bad_path", "src host path required")
    # Absolute or ~/… only — relative paths are ambiguous across MCP cwd.
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise OpsError(
            "bad_path",
            f"src must be an absolute host path (got {src!r}); "
            "use ~/… or /… — relative paths are rejected",
        )
    try:
        full = expanded.resolve(strict=True)
    except FileNotFoundError as e:
        raise OpsError("not_found", f"source file not found: {src}") from e
    except OSError as e:
        raise OpsError("bad_path", f"cannot resolve src: {e}") from e

    if not full.is_file():
        raise OpsError("bad_path", f"src is not a file: {src}")
    if full.suffix.lower() != ".md":
        raise OpsError("bad_path", f"src must be a .md file (got suffix {full.suffix!r})")

    vault_res = vault_root.expanduser().resolve()
    try:
        full.relative_to(vault_res)
    except ValueError:
        pass
    else:
        raise OpsError(
            "use_move_note",
            "src is already inside the vault; use move_note for rename/archive "
            "(or write_note / patch_note to edit in place)",
        )

    allowed = False
    for root in _send_allow_roots():
        try:
            full.relative_to(root)
            allowed = True
            break
        except ValueError:
            continue
    if not allowed:
        roots = ", ".join(str(r) for r in _send_allow_roots())
        raise OpsError(
            "forbidden_src",
            f"src {str(full)!r} is outside allowed roots ({roots}); "
            "set APO_SEND_ALLOW_ROOTS to extend",
        )

    try:
        size = full.stat().st_size
    except OSError as e:
        raise OpsError("bad_path", f"cannot stat src: {e}") from e
    if size > int(config.SEND_MAX_BYTES):
        raise OpsError(
            "too_large",
            f"src is {size} bytes; max APO_SEND_MAX_BYTES={config.SEND_MAX_BYTES}",
        )
    return full


def place_note(
    src: str,
    dst: str,
    *,
    overwrite: bool = False,
    fields: dict[str, Any] | None = None,
    expected_mtime: float | None = None,
    vault: str = "",
    ref: str = "",
    allow_cross_vault: bool = False,
) -> dict[str, Any]:
    """Place a note at ``dst``: move if ``src`` is in the vault, else copy from host.

    - Vault-relative ``src``, or absolute path under the vault root → ``moved``
      (same as former ``move_note``; ``fields`` not allowed).
    - Absolute host ``.md`` outside the vault (allow-roots) → ``copied`` / promote
      (same as former ``send_note``; leaves src; optional ``fields`` merge).
    - ``src``/``dst`` in different vaults → rejected unless ``allow_cross_vault=True``,
      in which case always ``copied`` (never moved) — src is left untouched, dst is
      OKF-validated against the *destination* vault's contract, optional ``fields`` merge.
    """
    bad = _reject_if_ref(ref, tool="place_note")
    if bad:
        return bad
    raw = (src or "").strip()
    if not raw:
        return _err(src=src, dst=dst, error="bad_path", message="src required")

    expanded = Path(raw).expanduser()
    try:
        # Prefer dst prefix (always vault-relative) for binding; peel vault-relative src too.
        if not expanded.is_absolute():
            b_src, raw = _resolve_binding_and_rel(raw, vault)
            b_dst, dst = _resolve_binding_and_rel(dst, vault, write=True)
            if b_src.name != b_dst.name:
                if not allow_cross_vault:
                    raise OpsError(
                        "bad_request",
                        f"src vault {b_src.name!r} conflicts with dst vault {b_dst.name!r} "
                        "(pass allow_cross_vault=true to copy across vaults)",
                    )
                if fields is not None and not isinstance(fields, dict):
                    raise OpsError("bad_request", "fields must be an object")
                src_root = b_src.resolved().root
                try:
                    src_full = _safe_resolve(src_root, raw)
                except ValueError as e:
                    raise OpsError("bad_path", str(e)) from e
                if not src_full.is_file():
                    raise OpsError("not_found", f"source not found: {b_src.name}:{raw}")
                out = _copy_into_vault(
                    src_full,
                    f"{b_src.name}:{raw}",
                    dst,
                    b_dst,
                    overwrite=overwrite,
                    fields=fields,
                    expected_mtime=expected_mtime,
                )
                if out.get("ok"):
                    out = dict(out)
                    out["mode"] = "copy"
                    out["cross_vault"] = True
                return out
            b = b_src
        else:
            b, dst = _resolve_binding_and_rel(dst, vault, write=True)
        root = b.resolved().root.resolve()
    except OpsError as e:
        return _err(src=src, dst=dst, error=e.code, message=e.message)
    in_vault_rel: str | None = None

    if expanded.is_absolute():
        try:
            full = expanded.resolve(strict=True)
        except FileNotFoundError:
            return _err(src=src, dst=dst, error="not_found", message=f"source not found: {src}")
        except OSError as e:
            return _err(src=src, dst=dst, error="bad_path", message=f"cannot resolve src: {e}")
        try:
            in_vault_rel = full.relative_to(root).as_posix()
        except ValueError:
            in_vault_rel = None
        if in_vault_rel is not None:
            if fields:
                return _err(
                    src=src,
                    dst=dst,
                    error="bad_request",
                    message="fields= is only for host→vault copy; omit when moving inside the vault",
                )
            out = move_note(
                in_vault_rel,
                dst,
                overwrite=overwrite,
                expected_mtime=expected_mtime,
                vault=b.name,
            )
            if out.get("ok"):
                out = dict(out)
                out["action"] = "moved"
                out["mode"] = "move"
            return out
        # Host copy path
        out = send_note(
            str(full),
            dst,
            overwrite=overwrite,
            fields=fields,
            expected_mtime=expected_mtime,
            vault=b.name,
        )
        if out.get("ok"):
            out = dict(out)
            # send_note already sets action created|overwrote
            out["mode"] = "copy"
        return out

    # Vault-relative move
    if fields:
        return _err(
            src=src,
            dst=dst,
            error="bad_request",
            message="fields= is only for host→vault copy; omit when moving inside the vault",
        )
    out = move_note(
        raw.replace("\\", "/"),
        dst,
        overwrite=overwrite,
        expected_mtime=expected_mtime,
        vault=b.name,
    )
    if out.get("ok"):
        out = dict(out)
        out["action"] = "moved"
        out["mode"] = "move"
    return out


def move_note(
    src: str,
    dst: str,
    *,
    overwrite: bool = False,
    expected_mtime: float | None = None,
    vault: str = "",
) -> dict[str, Any]:
    try:
        b_src, src = _resolve_binding_and_rel(src, vault)
        b_dst, dst = _resolve_binding_and_rel(dst, vault, write=True)
        if b_src.name != b_dst.name:
            raise OpsError(
                "bad_request",
                f"src vault {b_src.name!r} conflicts with dst vault {b_dst.name!r}",
            )
        b = b_src
        root = b.resolved().root
        src_full = _safe_resolve(root, src)
        dst_full = _safe_resolve(root, dst)
    except OpsError as e:
        return _err(src=src, dst=dst, error=e.code, message=e.message)
    except ValueError as e:
        return _err(src=src, dst=dst, error="bad_path", message=str(e))

    try:
        _require_note_path(src, root=root, full=src_full)
        _require_note_path(dst, root=root, full=dst_full)
    except OpsError as e:
        return _err(src=src, dst=dst, error=e.code, message=e.message)

    with note_write_lock(src_full, dst_full):
        if not src_full.exists():
            return _err(src=src, dst=dst, error="not_found", message=f"source note not found: {src}")
        if (guard := _check_mtime(src_full, expected_mtime, src)):
            return {**guard, "src": src, "dst": dst}
        if dst_full.exists() and not overwrite:
            return _err(
                src=src,
                dst=dst,
                error="destination_exists",
                message="pass overwrite=true to replace",
            )

        src_abs = str(src_full.resolve())
        try:
            dst_full.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src_full, dst_full)
        except OSError as e:
            return _io_error(e, dst_full, dst, src=src, dst=dst)

        purged = _enqueue_purge(b, Path(src_abs))
        index_deferred.requeue_move(b.collection, src_abs, str(dst_full.resolve()))

        out: dict[str, Any] = {
            "ok": True,
            "src": src,
            "dst": dst,
            "index_purged": purged,
            "mtime": _mtime(dst_full),
            "vault": b.name,
        }
        if not purged:
            out["warning"] = "purge not queued — watcher may retain stale chunks"
        return _finalize_write(
            out, vault=b.name, path=dst, expected_mtime=expected_mtime
        )


def _copy_into_vault(
    src_full: Path,
    src_display: str,
    dst: str,
    b: "vaults.VaultBinding",
    *,
    overwrite: bool = False,
    fields: dict[str, Any] | None = None,
    expected_mtime: float | None = None,
) -> dict[str, Any]:
    """Shared body for host->vault and vault->vault copy: read src_full, OKF-validate
    and write into vault ``b`` at ``dst``. ``src_full`` must already exist and be readable."""
    root = b.resolved().root
    try:
        dst_full = _safe_resolve(root, dst)
    except ValueError as e:
        return _err(src=src_display, dst=dst, error="bad_path", message=str(e))

    try:
        _require_note_path(dst, root=root, full=dst_full)
    except OpsError as e:
        return _err(src=src_display, dst=dst, error=e.code, message=e.message)

    with note_write_lock(dst_full):
        if (guard := _check_mtime(dst_full, expected_mtime, dst)):
            return {**guard, "src": src_display, "dst": dst}
        if dst_full.exists() and not overwrite:
            return _err(
                src=src_display,
                dst=dst,
                error="destination_exists",
                message="pass overwrite=true to replace",
            )

        try:
            content = src_full.read_text(encoding="utf-8")
        except OSError as e:
            return _err(src=src_display, dst=dst, error="bad_path", message=f"cannot read src: {e}")
        except UnicodeDecodeError as e:
            return _err(src=src_display, dst=dst, error="bad_path", message=f"src is not utf-8 text: {e}")

        fields_applied: list[str] = []
        if fields:
            if not isinstance(fields, dict):
                return _err(
                    src=src_display,
                    dst=dst,
                    error="bad_request",
                    message="fields must be an object of frontmatter key→value",
                )
            patch_ops = [
                {"op": "set_field", "field": str(k), "value": v}
                for k, v in fields.items()
            ]
            result = apply_patch(content, patch_ops, strict=False)
            if not result.ok and result.applied == 0:
                return _err(
                    src=src_display,
                    dst=dst,
                    applied=result.applied,
                    results=result.results,
                    **flatten_patch_failure_error(
                        result.error, suggestions=result.suggestions or None
                    ),
                )
            content = result.content
            fields_applied = [str(k) for k in fields]

        existed = dst_full.exists()
        parts = Path(dst.replace("\\", "/")).parts
        new_top = len(parts) > 1 and not (root / parts[0]).exists()

        okf = apo_okf.process_concept(vault_root=root, rel_path=dst, content=content)
        okf_meta = okf.as_response_fields()
        if not okf.ok:
            return _err(
                src=src_display,
                dst=dst,
                error=okf.error or "okf_validation",
                message=okf.message or "OKF validation failed",
                **{k: val for k, val in okf_meta.items() if k != "enforcement"},
                enforcement=okf.enforcement,
            )
        to_write = okf.content
        to_write, write_flaws = _prepare_write_content(
            to_write, path=dst, vault=b.name, okf_result=okf
        )

        try:
            atomic_write_text(dst_full, to_write)
        except OSError as e:
            return _io_error(e, dst_full, dst, src=src_display, dst=dst)
        _enqueue_index(b, dst_full)

        out: dict[str, Any] = {
            "ok": True,
            "src": src_display,
            "dst": dst,
            "action": "overwrote" if existed else "created",
            "bytes": dst_full.stat().st_size,
            "mtime": _mtime(dst_full),
            "fields_applied": fields_applied,
            "vault": b.name,
        }
        out.update(okf_meta)
        out = _attach_flaws(out, write_flaws)
        if new_top:
            out["warning"] = (
                f"created new top-level directory {parts[0]!r} — "
                f"existing top-level dirs: {_top_level_dirs(root)}"
            )
        return _finalize_write(
            out, vault=b.name, path=dst, expected_mtime=expected_mtime, content=to_write
        )


def send_note(
    src: str,
    dst: str,
    *,
    overwrite: bool = False,
    fields: dict[str, Any] | None = None,
    expected_mtime: float | None = None,
    vault: str = "",
) -> dict[str, Any]:
    """Copy a host .md file into the vault (optional frontmatter merge). Leaves src in place."""
    try:
        b, dst = _resolve_binding_and_rel(dst, vault, write=True)
        root = b.resolved().root
        src_full = _resolve_send_src(src, root)
    except OpsError as e:
        return _err(src=src, dst=dst, error=e.code, message=e.message)
    except ValueError as e:
        return _err(src=src, dst=dst, error="bad_path", message=str(e))

    return _copy_into_vault(
        src_full,
        str(src_full),
        dst,
        b,
        overwrite=overwrite,
        fields=fields,
        expected_mtime=expected_mtime,
    )


def delete_note(
    path: str,
    *,
    expected_mtime: float | None = None,
    vault: str = "",
    ref: str = "",
) -> dict[str, Any]:
    bad = _reject_if_ref(ref, tool="delete_note")
    if bad:
        return bad
    try:
        b, path = _resolve_binding_and_rel(path, vault, write=True)
        root = b.resolved().root
        full = _safe_resolve(root, path)
    except OpsError as e:
        return _err(path=path, error=e.code, message=e.message)
    except ValueError as e:
        return _err(path=path, error="bad_path", message=str(e))

    try:
        _require_note_path(path, root=root, full=full)
    except OpsError as e:
        return _err(path=path, error=e.code, message=e.message)

    with note_write_lock(full):
        if not full.exists():
            return _err(path=path, error="not_found", message="note not found")
        if guard := _check_mtime(full, expected_mtime, path):
            return guard
        abs_path = str(full.resolve())
        full.unlink()
        purged = _enqueue_purge(b, full)
        index_deferred.dequeue_paths(b.collection, [abs_path])
    out: dict[str, Any] = {
        "ok": True,
        "path": path,
        "index_purged": purged,
        "vault": b.name,
    }
    if not purged:
        out["warning"] = "purge not queued — watcher may retain stale chunks"
    return _stamp_qualified(
        _attach_watcher_tip(out), vault=b.name, path=path
    )
