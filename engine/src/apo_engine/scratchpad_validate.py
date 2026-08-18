"""Scratchpad validation: format parse, vaulted JSON Schema, okf type_profiles."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from apo_engine.okf import resolve_contract_path
from apo_engine.scratchpad_format import _diag, buffer_as_dict, normalize_buffer
from apo_engine.scratchpad_store import Format, ScratchpadMeta

SCHEMA_PREFIX = "system/schemas/"


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def resolve_schema_file(
    vault_root: Path,
    schema_path: str,
    *,
    allow_foreign: bool,
) -> tuple[Path | None, dict[str, Any] | None]:
    rel = schema_path.strip().lstrip("/")
    if not rel:
        return None, _diag("ERROR", "BAD_SCHEMA_PATH", "schema_path", "schema_path is empty")
    if not allow_foreign and not rel.startswith(SCHEMA_PREFIX):
        return None, _diag(
            "ERROR",
            "FOREIGN_SCHEMA",
            rel,
            f"schema_path must live under {SCHEMA_PREFIX} (pass allow_foreign_schema=true to opt in).",
            hint=f"Move the schema to {SCHEMA_PREFIX} or set allow_foreign_schema=true.",
        )
    path = (vault_root / rel).resolve()
    try:
        path.relative_to(vault_root.resolve())
    except ValueError:
        return None, _diag("ERROR", "SCHEMA_ESCAPE", rel, "schema_path escapes the vault root")
    if not path.is_file():
        return None, _diag("ERROR", "SCHEMA_MISSING", rel, f"Schema file not found: {rel}")
    return path, None


def load_json_schema(path: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return None, _diag("ERROR", "SCHEMA_LOAD", str(path), str(e))
    if not isinstance(data, dict):
        return None, _diag("ERROR", "SCHEMA_ROOT", str(path), "JSON Schema root must be an object")
    return data, None


def _local_registry(vault_root: Path, schema_file: Path, schema: dict[str, Any]) -> Any:
    """Build a referencing.Registry that only resolves vault-local file refs."""
    try:
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
    except ImportError:
        return None

    vault_root = vault_root.resolve()

    def _retrieve(uri: str) -> Resource:
        if uri.startswith(("http://", "https://")):
            raise ValueError(f"remote $ref not allowed: {uri}")
        # file:// or relative
        if uri.startswith("file://"):
            from pathlib import Path as P

            path = P(uri.removeprefix("file://"))
        else:
            path = (schema_file.parent / uri).resolve()
        try:
            path.relative_to(vault_root)
        except ValueError as e:
            raise ValueError(f"$ref escapes vault root: {uri}") from e
        data = json.loads(path.read_text(encoding="utf-8"))
        return Resource.from_contents(data, default_specification=DRAFT202012)

    base = Resource.from_contents(schema, default_specification=DRAFT202012)
    return Registry(retrieve=_retrieve).with_resource(uri=schema_file.as_uri(), resource=base)


def validate_json_schema(
    instance: Any,
    schema: dict[str, Any],
    *,
    vault_root: Path,
    schema_file: Path,
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        diagnostics.append(
            _diag(
                "ERROR",
                "JSONSCHEMA_MISSING",
                "schema",
                "jsonschema package is not installed",
                hint="pip install jsonschema",
            )
        )
        return diagnostics

    registry = _local_registry(vault_root, schema_file, schema)
    try:
        if registry is not None:
            validator = Draft202012Validator(schema, registry=registry)
        else:
            validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    except Exception as e:
        diagnostics.append(_diag("ERROR", "SCHEMA_VALIDATE", "$", str(e)))
        return diagnostics

    for err in errors:
        path = ".".join(str(p) for p in err.absolute_path) or "$"
        diagnostics.append(
            _diag("ERROR", "SCHEMA_ERROR", path, err.message, hint="Fix the field or rebind schema.")
        )
    return diagnostics


def load_type_profile(vault_root: Path, schema_type: str) -> dict[str, Any] | None:
    contract_path = resolve_contract_path(vault_root)
    if contract_path is None:
        return None
    try:
        data = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    profiles = data.get("type_profiles") or {}
    if not isinstance(profiles, dict):
        return None
    profile = profiles.get(schema_type)
    return profile if isinstance(profile, dict) else None


def validate_type_profile(instance: dict[str, Any], schema_type: str, profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Best-effort shape checks for okf type_profiles (e.g. Plan todos)."""
    diagnostics: list[dict[str, Any]] = []
    todos_spec = profile.get("todos")
    if isinstance(todos_spec, dict) and "todos" in instance:
        todos = instance.get("todos")
        if not isinstance(todos, list):
            diagnostics.append(_diag("ERROR", "PROFILE_TODOS", "todos", "todos must be a list"))
        else:
            statuses = set(todos_spec.get("item_status") or [])
            for i, item in enumerate(todos):
                if not isinstance(item, dict):
                    diagnostics.append(_diag("ERROR", "PROFILE_TODO_ITEM", f"todos[{i}]", "todo item must be an object"))
                    continue
                for req in ("id", "content", "status"):
                    if req not in item:
                        diagnostics.append(
                            _diag("ERROR", "PROFILE_TODO_FIELD", f"todos[{i}].{req}", f"missing required field {req!r}")
                        )
                st = item.get("status")
                if statuses and st is not None and str(st) not in statuses:
                    diagnostics.append(
                        _diag(
                            "ERROR",
                            "PROFILE_TODO_STATUS",
                            f"todos[{i}].status",
                            f"status {st!r} not in {sorted(statuses)}",
                        )
                    )
    progress_spec = profile.get("progress")
    if isinstance(progress_spec, dict) and "progress" in instance:
        prog = instance.get("progress")
        if not isinstance(prog, dict):
            diagnostics.append(_diag("ERROR", "PROFILE_PROGRESS", "progress", "progress must be an object"))
    note_status = profile.get("note_status")
    if isinstance(note_status, list) and "status" in instance:
        if str(instance.get("status")) not in {str(x) for x in note_status}:
            diagnostics.append(
                _diag(
                    "ERROR",
                    "PROFILE_STATUS",
                    "status",
                    f"status {instance.get('status')!r} not in {note_status}",
                )
            )
    return diagnostics


def handoff_paths_from_schema(schema: dict[str, Any] | None) -> list[str] | None:
    if not schema:
        return None
    ext = schema.get("x-apo-handoff")
    if isinstance(ext, list) and all(isinstance(x, str) for x in ext):
        return list(ext)
    return None


def validate_session(
    meta: ScratchpadMeta,
    content: str,
    *,
    vault_root: Path | None,
) -> dict[str, Any]:
    """Return envelope with valid + diagnostics (format + bound schemas)."""
    diagnostics: list[dict[str, Any]] = []
    _, format_diags = normalize_buffer(meta.format, content)
    diagnostics.extend(format_diags)

    instance = buffer_as_dict(meta.format, content)
    schema_obj: dict[str, Any] | None = None

    if meta.schema_path:
        if vault_root is None:
            diagnostics.append(
                _diag("ERROR", "VAULT_REQUIRED", "vault", "vault= is required to bind schema_path")
            )
        else:
            path, err = resolve_schema_file(
                vault_root, meta.schema_path, allow_foreign=meta.allow_foreign_schema
            )
            if err:
                diagnostics.append(err)
            elif path is not None:
                schema_obj, load_err = load_json_schema(path)
                if load_err:
                    diagnostics.append(load_err)
                elif schema_obj is not None:
                    if meta.schema_hash and file_sha256(path) != meta.schema_hash:
                        diagnostics.append(
                            _diag(
                                "WARNING",
                                "SCHEMA_CHANGED",
                                meta.schema_path,
                                "Bound schema file content hash changed since bind",
                            )
                        )
                    if instance is None:
                        diagnostics.append(
                            _diag("ERROR", "INSTANCE_UNPARSED", "$", "Cannot validate unparsed buffer against schema")
                        )
                    else:
                        diagnostics.extend(
                            validate_json_schema(
                                instance, schema_obj, vault_root=vault_root, schema_file=path
                            )
                        )

    if meta.schema_type:
        if vault_root is None:
            diagnostics.append(
                _diag("ERROR", "VAULT_REQUIRED", "vault", "vault= is required to bind schema_type")
            )
        else:
            profile = load_type_profile(vault_root, meta.schema_type)
            if profile is None:
                diagnostics.append(
                    _diag(
                        "ERROR",
                        "UNKNOWN_SCHEMA_TYPE",
                        meta.schema_type,
                        f"type_profile {meta.schema_type!r} not found in okf-contract",
                    )
                )
            elif isinstance(instance, dict):
                diagnostics.extend(validate_type_profile(instance, meta.schema_type, profile))
            else:
                diagnostics.append(
                    _diag("ERROR", "INSTANCE_UNPARSED", "$", "Cannot validate type_profile against non-object buffer")
                )

    errors = [d for d in diagnostics if d.get("severity") == "ERROR"]
    return {
        "valid": len(errors) == 0,
        "diagnostics": diagnostics,
        "handoff_paths": handoff_paths_from_schema(schema_obj),
    }
