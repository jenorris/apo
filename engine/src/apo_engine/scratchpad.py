"""Scratchpad MCP/RPC actions: create/checkout/read/patch/validate/bind_schema/commit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from apo_engine import vaults
from apo_engine.scratchpad_format import (
    apply_ops_to_buffer,
    buffer_as_dict,
    content_hash,
    fragment_view,
    handoff_view,
    normalize_buffer,
    section_hashes,
    toc_view,
)
from apo_engine.scratchpad_merge import merge_buffers
from apo_engine.scratchpad_store import (
    Format,
    ScratchpadMeta,
    discard_session,
    load_session,
    new_session_id,
    save_session,
    status_envelope,
)
from apo_engine.scratchpad_validate import (
    file_sha256,
    load_json_schema,
    resolve_schema_file,
    validate_session,
)


def _bad(error: str, message: str, **extra: Any) -> dict[str, Any]:
    out = {"ok": False, "error": error, "message": message}
    out.update(extra)
    return out


def _normalize_format(raw: str | None) -> Format | None:
    if not raw:
        return "markdown"
    v = str(raw).strip().lower()
    if v in ("md", "markdown"):
        return "markdown"
    if v in ("yml", "yaml"):
        return "yaml"
    if v == "json":
        return "json"
    return None


def _load_or_err(session_id: str) -> tuple[ScratchpadMeta, str] | dict[str, Any]:
    hit = load_session(session_id)
    if hit is None:
        return _bad("not_found", f"scratchpad session {session_id!r} not found or expired")
    return hit


def _safe_vault_file(root: Path, rel: str) -> Path:
    """Resolve vault-relative path; raise ValueError on traversal."""
    full = (root / rel).resolve()
    full.relative_to(root.resolve())
    return full


def _vault_root(vault: str | None) -> tuple[Path | None, str | None, dict[str, Any] | None]:
    if not vault:
        return None, None, None
    try:
        default, bindings = vaults.load_bindings()
    except ValueError as e:
        return None, None, _bad("bad_vault", str(e))
    name = (vault or "").strip() or default
    b = bindings.get(name)
    if b is None:
        return None, None, _bad("bad_vault", f"unknown vault {name!r}")
    return Path(b.root), b.name, None


def scratchpad_op(
    action: str,
    *,
    session_id: str | None = None,
    format: str | None = None,
    content: Any = None,
    vault: str = "",
    vault_path: str | None = None,
    schema_path: str | None = None,
    schema_type: str | None = None,
    ops: list[dict[str, Any]] | None = None,
    destination_path: str | None = None,
    include: list[str] | None = None,
    view: str | None = None,
    json_path: str | None = None,
    heading: str | None = None,
    fields: list[str] | None = None,
    region: str | None = None,
    validate: bool | None = None,
    allow_foreign_schema: bool = False,
    allow_cross_vault_schema: bool = False,
) -> dict[str, Any]:
    act = (action or "").strip().lower()
    if act == "create":
        return _create(
            format=format,
            content=content,
            vault=vault,
            schema_path=schema_path,
            schema_type=schema_type,
            allow_foreign_schema=allow_foreign_schema,
            allow_cross_vault_schema=allow_cross_vault_schema,
        )
    if act == "checkout":
        return _checkout(
            vault_path=vault_path or "",
            vault=vault,
            schema_path=schema_path,
            schema_type=schema_type,
            allow_foreign_schema=allow_foreign_schema,
            allow_cross_vault_schema=allow_cross_vault_schema,
        )
    if not session_id:
        return _bad("bad_request", "session_id is required for this action")

    loaded = _load_or_err(session_id)
    if isinstance(loaded, dict):
        return loaded
    meta, buf = loaded

    if act == "status":
        return status_envelope(meta)

    if act == "discard":
        discard_session(session_id)
        return {"ok": True, "discarded": session_id}

    if act == "read":
        return _read(
            meta,
            buf,
            include=include,
            view=view,
            json_path=json_path,
            heading=heading,
            fields=fields,
            region=region,
        )

    if meta.state == "PROMOTED" and act in {"patch", "bind_schema"}:
        return _bad(
            "promoted",
            f"Session {meta.session_id} promoted to {meta.promoted_path!r}. "
            "Mutation denied. Issue scratchpad(action=checkout|create) to fork.",
        )

    if act == "patch":
        return _patch(meta, buf, ops=ops or [], validate=validate)
    if act == "validate":
        return _validate(meta, buf)
    if act == "bind_schema":
        return _bind_schema(
            meta,
            buf,
            vault=vault or meta.vault or "",
            schema_path=schema_path,
            schema_type=schema_type,
            allow_foreign_schema=allow_foreign_schema,
            allow_cross_vault_schema=allow_cross_vault_schema,
        )
    if act == "commit":
        if allow_cross_vault_schema:
            meta.allow_cross_vault_schema = True
        if allow_foreign_schema:
            meta.allow_foreign_schema = True
        return _commit(
            meta,
            buf,
            destination_path=destination_path or meta.destination_path,
            vault=vault or meta.vault or "",
        )
    return _bad("bad_action", f"unknown scratchpad action {action!r}")


def _create(
    *,
    format: str | None,
    content: Any,
    vault: str,
    schema_path: str | None,
    schema_type: str | None,
    allow_foreign_schema: bool,
    allow_cross_vault_schema: bool,
) -> dict[str, Any]:
    fmt = _normalize_format(format)
    if fmt is None:
        return _bad("bad_request", f"unsupported format {format!r}")
    if (schema_path or schema_type) and not vault:
        return _bad("bad_request", "vault= is required when binding schema_path or schema_type")
    text, diags = normalize_buffer(fmt, content if content is not None else ({} if fmt == "json" else ""))
    sid = new_session_id()
    meta = ScratchpadMeta(
        session_id=sid,
        format=fmt,
        state="ACTIVE",
        vault=vault or None,
        allow_foreign_schema=allow_foreign_schema,
        allow_cross_vault_schema=allow_cross_vault_schema,
    )
    save_session(meta, text)
    out = status_envelope(meta, diagnostics=diags)
    if schema_path is not None or schema_type is not None:
        bound = _bind_schema(
            meta,
            text,
            vault=vault,
            schema_path=schema_path,
            schema_type=schema_type,
            allow_foreign_schema=allow_foreign_schema,
            allow_cross_vault_schema=allow_cross_vault_schema,
        )
        if content is not None and isinstance(content, str) and len(content) > 256:
            bound = dict(bound)
            bound["tip"] = (
                "schema-bound session: prefer patch(ops=[set_field…]) over "
                "re-create with full content="
            )
        return bound
    return out


def _checkout(
    *,
    vault_path: str,
    vault: str,
    schema_path: str | None,
    schema_type: str | None,
    allow_foreign_schema: bool,
    allow_cross_vault_schema: bool,
) -> dict[str, Any]:
    if not vault_path:
        return _bad("bad_request", "vault_path is required for checkout")
    root, vname, err = _vault_root(vault)
    if err:
        return err
    if root is None:
        return _bad("bad_request", "vault= is required for checkout")
    try:
        path = _safe_vault_file(root, vault_path)
    except ValueError:
        return _bad("bad_path", f"vault_path escapes vault root: {vault_path}")
    if not path.is_file():
        return _bad("not_found", f"vault path not found: {vault_path}")
    raw = path.read_text(encoding="utf-8")
    # Persist vault-relative path without .. segments
    vault_path = str(path.relative_to(root.resolve())).replace("\\", "/")
    suffix = path.suffix.lower()
    fmt: Format = "markdown"
    if suffix in {".yaml", ".yml"}:
        fmt = "yaml"
    elif suffix == ".json":
        fmt = "json"
    text, diags = normalize_buffer(fmt, raw)
    sid = new_session_id()
    meta = ScratchpadMeta(
        session_id=sid,
        format=fmt,
        state="ACTIVE",
        vault=vname,
        source_path=vault_path,
        destination_path=vault_path,
        base_content_hash=content_hash(text),
        base_section_hashes=section_hashes(text) if fmt == "markdown" else {},
        allow_foreign_schema=allow_foreign_schema,
        allow_cross_vault_schema=allow_cross_vault_schema,
    )
    save_session(meta, text)
    persist_base_snapshot(sid, text)
    out = status_envelope(meta, diagnostics=diags)
    if schema_path is not None or schema_type is not None:
        return _bind_schema(
            meta,
            text,
            vault=vname or vault,
            schema_path=schema_path,
            schema_type=schema_type,
            allow_foreign_schema=allow_foreign_schema,
            allow_cross_vault_schema=allow_cross_vault_schema,
        )
    return out


def _session_base_path(session_id: str) -> Path:
    from apo_engine.scratchpad_store import _session_dir

    return _session_dir(session_id) / "base.txt"


def persist_base_snapshot(session_id: str, content: str) -> None:
    path = _session_base_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _include_payload(
    meta: ScratchpadMeta,
    content: str,
    include: list[str] | None,
    *,
    view: str | None = None,
    json_path: str | None = None,
    heading: str | None = None,
    fields: list[str] | None = None,
    region: str | None = None,
    handoff_paths: list[str] | None = None,
) -> dict[str, Any]:
    wanted = set(include or [])
    if view:
        wanted.add(view)
    # Default view
    if not wanted:
        data = buffer_as_dict(meta.format, content)
        if isinstance(data, dict) and (
            meta.schema_type
            or data.get("okf_type") == "Plan"
            or "todos" in data
        ):
            wanted.add("handoff")
        else:
            return {}
    payload: dict[str, Any] = {}
    if "buffer" in wanted or "raw" in wanted:
        text = content
        if len(text.encode("utf-8")) > 8 * 1024:
            payload["buffer"] = text[: 8 * 1024]
            payload["tip"] = "buffer truncated (>8KiB); use include=fragment|handoff"
        else:
            payload["buffer"] = text
    if "handoff" in wanted:
        payload["handoff"] = handoff_view(meta.format, content, handoff_paths)
    if "fragment" in wanted or json_path or heading or fields or region:
        payload["fragment"] = fragment_view(
            meta.format,
            content,
            json_path=json_path,
            heading=heading,
            fields=fields,
            region=region,
        )
    if "toc" in wanted and meta.format == "markdown":
        payload["toc"] = toc_view(content)
    return payload


def _read(
    meta: ScratchpadMeta,
    content: str,
    **kwargs: Any,
) -> dict[str, Any]:
    if meta.state == "PROMOTED" and meta.promoted_path and meta.vault:
        root, _, err = _vault_root(meta.vault)
        if err:
            return err
        if root is not None:
            path = root / meta.promoted_path
            if path.is_file():
                content = path.read_text(encoding="utf-8")
    handoff_paths = None
    if meta.schema_path and meta.vault:
        root, _, err = _vault_root(meta.vault)
        if err is None and root is not None:
            path, serr = resolve_schema_file(
                root, meta.schema_path, allow_foreign=meta.allow_foreign_schema
            )
            if path is not None and serr is None:
                schema_obj, _ = load_json_schema(path)
                from apo_engine.scratchpad_validate import handoff_paths_from_schema

                handoff_paths = handoff_paths_from_schema(schema_obj)
    out = status_envelope(meta)
    out.update(
        _include_payload(
            meta,
            content,
            kwargs.get("include"),
            view=kwargs.get("view"),
            json_path=kwargs.get("json_path"),
            heading=kwargs.get("heading"),
            fields=kwargs.get("fields"),
            region=kwargs.get("region"),
            handoff_paths=handoff_paths,
        )
    )
    return out


def _patch(
    meta: ScratchpadMeta,
    content: str,
    *,
    ops: list[dict[str, Any]],
    validate: bool | None,
) -> dict[str, Any]:
    if not ops:
        return _bad("bad_request", "ops[] is required for patch")
    normalized: list[dict[str, Any]] = []
    for op in ops:
        if isinstance(op, dict):
            normalized.append(op)
        elif hasattr(op, "model_dump"):
            normalized.append(op.model_dump(mode="python", exclude_none=True))
        else:
            return _bad("bad_request", f"unsupported patch op type: {type(op)!r}")
    # Reject multi-path items batches
    if any("path" in op for op in normalized):
        return _bad("bad_request", "scratchpad patch is single-buffer; do not pass path on ops")
    new_content, results, ok = apply_ops_to_buffer(meta.format, content, normalized)
    if not ok:
        return _bad("patch_failed", "one or more ops failed", results=results, session_id=meta.session_id)
    meta.state = "STAGED"
    save_session(meta, new_content)
    out = status_envelope(meta, applied=[{"op": r.get("op"), "field": r.get("field")} for r in results if isinstance(r, dict)])
    out["results"] = results
    should_validate = validate if validate is not None else bool(meta.schema_path or meta.schema_type)
    if should_validate:
        v = _validate(meta, new_content)
        out["valid"] = v.get("valid")
        out["diagnostics"] = v.get("diagnostics")
        if v.get("ok") is False:
            out["ok"] = False
    return out


def _validate(meta: ScratchpadMeta, content: str) -> dict[str, Any]:
    root = None
    if meta.vault:
        root, _, err = _vault_root(meta.vault)
        if err:
            return err
    result = validate_session(meta, content, vault_root=root)
    if result["valid"]:
        meta.state = "VALID"
    else:
        meta.state = "STAGED"
    save_session(meta, content)
    out = status_envelope(meta, valid=result["valid"], diagnostics=result["diagnostics"])
    return out


def _bind_schema(
    meta: ScratchpadMeta,
    content: str,
    *,
    vault: str,
    schema_path: str | None,
    schema_type: str | None,
    allow_foreign_schema: bool,
    allow_cross_vault_schema: bool,
) -> dict[str, Any]:
    if not vault:
        return _bad("bad_request", "vault= is required for bind_schema")
    root, vname, err = _vault_root(vault)
    if err:
        return err
    assert root is not None
    meta.vault = vname
    meta.allow_foreign_schema = allow_foreign_schema or meta.allow_foreign_schema
    meta.allow_cross_vault_schema = allow_cross_vault_schema or meta.allow_cross_vault_schema

    if schema_path is not None:
        if schema_path == "":
            meta.schema_path = None
            meta.schema_hash = None
            meta.schema_vault = None
        else:
            path, serr = resolve_schema_file(
                root, schema_path, allow_foreign=meta.allow_foreign_schema
            )
            if serr:
                return _bad(serr["code"].lower(), serr["message"], diagnostics=[serr])
            assert path is not None
            schema, load_err = load_json_schema(path)
            if load_err:
                return _bad(load_err["code"].lower(), load_err["message"], diagnostics=[load_err])
            meta.schema_path = schema_path.strip().lstrip("/")
            meta.schema_vault = vname
            meta.schema_hash = file_sha256(path)

    if schema_type is not None:
        meta.schema_type = schema_type or None
        if meta.schema_type and meta.schema_vault is None:
            meta.schema_vault = vname

    save_session(meta, content)
    return _validate(meta, content)


def _commit(
    meta: ScratchpadMeta,
    content: str,
    *,
    destination_path: str | None,
    vault: str,
) -> dict[str, Any]:
    if not destination_path:
        return _bad("bad_request", "destination_path is required for commit")
    if not vault:
        return _bad("bad_request", "vault= is required for commit")
    root, vname, err = _vault_root(vault)
    if err:
        return err
    assert root is not None

    if meta.schema_vault and meta.schema_vault != vname and not meta.allow_cross_vault_schema:
        return _bad(
            "cross_vault_schema",
            f"buffer validated with schema_vault={meta.schema_vault!r} but commit targets vault={vname!r}",
            tip="Pass allow_cross_vault_schema=true to override.",
        )

    # Always re-validate
    meta.vault = vname
    v = validate_session(meta, content, vault_root=root)
    if not v["valid"]:
        return status_envelope(
            meta,
            ok=False,
            error="validation_failed",
            valid=False,
            diagnostics=v["diagnostics"],
            message="commit refused: validation errors",
        )

    try:
        dest = _safe_vault_file(root, destination_path)
    except ValueError:
        return _bad("bad_path", f"destination_path escapes vault root: {destination_path}")
    destination_path = str(dest.relative_to(root.resolve())).replace("\\", "/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    theirs = dest.read_text(encoding="utf-8") if dest.is_file() else ""

    base_text = ""
    if meta.base_content_hash:
        base_path = _session_base_path(meta.session_id)
        if base_path.is_file():
            base_text = base_path.read_text(encoding="utf-8")
        elif theirs and content_hash(theirs) == meta.base_content_hash:
            base_text = theirs
        elif theirs and theirs != content:
            return _bad(
                "merge_base_missing",
                "Cannot 3-way merge: checkout base text was not persisted. Re-checkout and retry.",
            )
        else:
            base_text = theirs
    else:
        base_text = theirs  # create-then-commit with no prior checkout base

    merged, conflicts = merge_buffers(fmt=meta.format, base=base_text, ours=content, theirs=theirs)
    if conflicts:
        return status_envelope(
            meta,
            ok=False,
            error="merge_conflict",
            diagnostics=[{"severity": "ERROR", **c} for c in conflicts],
            message="MERGE_CONFLICT",
        )
    assert merged is not None

    # CAS: re-read trunk before write
    again = dest.read_text(encoding="utf-8") if dest.is_file() else ""
    if again != theirs:
        return _bad("stale_write", "destination changed during merge; retry commit")

    from apo_engine import ops as apo_ops

    written = apo_ops.write_note(destination_path, content=merged, vault=vname or "")
    if not written.get("ok"):
        return {
            **status_envelope(meta),
            "ok": False,
            "error": written.get("error") or "write_failed",
            "message": written.get("message") or "commit write failed",
            "write": written,
        }

    meta.state = "PROMOTED"
    meta.promoted_path = destination_path
    meta.destination_path = destination_path
    meta.vault = vname
    save_session(meta, merged)
    out = status_envelope(meta, committed=destination_path, vault=vname)
    out["write"] = written
    return out