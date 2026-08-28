"""Scratchpad MCP/RPC actions: create/read/patch/commit/discard."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apo_engine import vaults
from apo_engine import yaml_rt as _yaml_rt
from apo_engine.scratchpad_format import _diag, apply_ops_to_buffer, normalize_buffer
from apo_engine.scratchpad_store import (
    Format,
    ScratchpadMeta,
    discard_session,
    load_session,
    new_session_id,
    read_buffer_payload,
    save_session,
    status_envelope,
)
from apo_engine.scratchpad_validate import validate_buffer


def _bad(error: str, message: str, **extra: Any) -> dict[str, Any]:
    out = {"ok": False, "error": error, "message": message}
    out.update(extra)
    return out


def _normalize_format(raw: str | None) -> Format | None:
    if not raw:
        return "json"
    v = str(raw).strip().lower()
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
    schema_path: str | None = None,
    schema_type: str | None = None,
    ops: list[dict[str, Any]] | None = None,
    destination_path: str | None = None,
) -> dict[str, Any]:
    act = (action or "").strip().lower()
    if act == "create":
        return _create(format=format, content=content)
    if not session_id:
        return _bad("bad_request", "session_id is required for this action")

    if act == "discard":
        discard_session(session_id)
        return {"ok": True, "discarded": session_id}

    loaded = _load_or_err(session_id)
    if isinstance(loaded, dict):
        return loaded
    meta, buf = loaded

    if act == "read":
        out = status_envelope(meta)
        out.update(read_buffer_payload(buf))
        return out

    if meta.state == "PROMOTED" and act == "patch":
        return _bad(
            "promoted",
            f"Session {meta.session_id} promoted to {meta.promoted_path!r}. "
            "Mutation denied — create a new session to iterate.",
        )

    if act == "patch":
        return _patch(meta, buf, ops=ops or [])
    if act == "commit":
        return commit_session(
            meta,
            buf,
            destination_path=destination_path or meta.destination_path or "",
            vault=vault or meta.vault or "",
            schema_path=schema_path,
            schema_type=schema_type,
        )
    return _bad("bad_action", f"unknown scratchpad action {action!r}")


def _persist(fmt: Format, text: str, diags: list[dict[str, Any]]) -> dict[str, Any]:
    sid = new_session_id()
    meta = ScratchpadMeta(session_id=sid, format=fmt, state="ACTIVE")
    save_session(meta, text)
    return status_envelope(meta, diagnostics=diags)


def _create(*, format: str | None, content: Any) -> dict[str, Any]:
    fmt = _normalize_format(format)
    if fmt is None:
        return _bad("bad_request", f"unsupported format {format!r}; use json or yaml")
    if content is None:
        content = {} if fmt == "json" else ""

    # Explicit format (including explicit "json") keeps the strict contract:
    # a broken payload is preserved as-is with diagnostics so the caller can
    # recover/repair it rather than losing their seed text (a documented,
    # deliberate fallback for payloads already labeled as their format).
    if format is not None:
        text, diags = normalize_buffer(fmt, content)
        return _persist(fmt, text, diags)

    # Omitted format defaults to JSON, but a seed that does not parse as JSON
    # is far more likely an unlabeled YAML blob or prose/markdown than a broken
    # JSON payload. Never silently persist a poisoned buffer that patch() and
    # commit() would both reject later: try YAML, then fail with guidance.
    text, diags = normalize_buffer("json", content)
    if not any(d.get("severity") == "ERROR" for d in diags):
        return _persist("json", text, diags)
    if isinstance(content, str) and content.strip():
        yd = _yaml_rt.load(content)
        if isinstance(yd, dict):
            return _persist("yaml", _yaml_rt.dump(yd), [])
    return _bad(
        "create_failed",
        "content does not parse as a JSON payload (scratchpad sessions hold "
        "JSON/YAML payloads only, not prose/markdown). Pass format='yaml' for "
        "a YAML blob, or stage markdown note content via write_note.",
        diagnostics=diags,
        hint="scratchpad is a JSON/YAML payload workshop; markdown notes go through write_note.",
    )


def _patch(meta: ScratchpadMeta, content: str, *, ops: list[dict[str, Any]]) -> dict[str, Any]:
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
    if any("path" in op for op in normalized):
        return _bad("bad_request", "scratchpad patch is single-buffer; do not pass path on ops")
    new_content, results, ok = apply_ops_to_buffer(meta.format, content, normalized)
    if not ok:
        return _bad("patch_failed", "one or more ops failed", results=results, session_id=meta.session_id)
    meta.state = "STAGED"
    save_session(meta, new_content)
    out = status_envelope(meta, applied=[{"op": r.get("op"), "field": r.get("field")} for r in results if isinstance(r, dict)])
    out["results"] = results
    return out


def commit_session(
    meta: ScratchpadMeta,
    content: str,
    *,
    destination_path: str,
    vault: str,
    schema_path: str | None = None,
    schema_type: str | None = None,
) -> dict[str, Any]:
    """Promote spill buffer to a vault path (overwrite; optional schema validate)."""
    if not destination_path:
        return _bad("bad_request", "destination_path is required for commit")
    if not vault:
        return _bad("bad_request", "vault= is required for commit")
    root, vname, err = _vault_root(vault)
    if err:
        return err
    assert root is not None

    meta.vault = vname
    v = validate_buffer(
        meta.format,
        content,
        vault_root=root,
        schema_path=schema_path,
        schema_type=schema_type,
    )
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

    from apo_engine import ops as apo_ops

    written = apo_ops.write_note(
        destination_path,
        content=content,
        vault=vname or "",
        catalog_format=meta.format,
    )
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
    save_session(meta, content)
    out = status_envelope(meta, committed=destination_path, vault=vname)
    out["write"] = written
    return out
