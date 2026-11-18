"""Conservative MCP argument coercion: unambiguous near-misses only.

Applied before FastMCP validates tool arguments, so a call that differs from
the schema in one obvious way succeeds instead of costing the agent a round
trip. Anything ambiguous (alias and canonical name both given, two aliases for
one parameter, malformed JSON) is left untouched and fails normal validation.

This module has no FastMCP imports so ``tool_metrics`` and unit tests can call
:func:`coerce_arguments` directly; the middleware is in ``arg_coercion_middleware``.
"""

from __future__ import annotations

import json
from typing import Any, Collection

from apo_engine.tool_aliases import canonical_tool

# tool -> {canonical parameter: aliases}. An alias is renamed only when the
# canonical name is absent and exactly one of its aliases is present.
_RENAMES: dict[str, dict[str, tuple[str, ...]]] = {
    "search_notes": {"limit": ("k", "max_results", "num_results")},
    "filter_notes": {"limit": ("k", "max_results", "num_results")},
    "patch_note": {"ops": ("operations", "edits", "updates")},
    "write_note": {"content": ("body", "text")},
    "append_note": {"text": ("content", "body")},
}

# tool -> parameters that take a JSON object but sometimes arrive as a JSON string.
_JSON_OBJECT_PARAMS: dict[str, tuple[str, ...]] = {
    "filter_notes": ("where",),
    "vault": ("request",),
}


def _parse_json(value: Any, kinds: tuple[type, ...]) -> Any:
    """``value`` parsed from a JSON string when it decodes to one of ``kinds``; else ``value``."""
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    return parsed if isinstance(parsed, kinds) else value


def optional_non_nullable(schema: dict[str, Any] | None) -> frozenset[str]:
    """Parameters of a tool input schema that are optional and reject ``null``.

    A ``null`` for one of these can only mean "not set", so it is safe to drop.
    Nullable parameters already validate and are left alone.
    """
    if not isinstance(schema, dict):
        return frozenset()
    props = schema.get("properties")
    if not isinstance(props, dict):
        return frozenset()
    required = set(schema.get("required") or ())
    out: set[str] = set()
    for key, spec in props.items():
        if key in required or not isinstance(spec, dict):
            continue
        variants = [spec, *(spec.get("anyOf") or ()), *(spec.get("oneOf") or ())]
        types: set[str] = set()
        for v in variants:
            t = v.get("type") if isinstance(v, dict) else None
            types.update(t if isinstance(t, list) else [t] if t else [])
        # No declared type at all = accepts anything, null included.
        if types and "null" not in types:
            out.add(str(key))
    return frozenset(out)


def coerce_arguments(
    tool: str,
    arguments: dict[str, Any] | None,
    *,
    droppable_none: Collection[str] = (),
) -> tuple[dict[str, Any], bool]:
    """Apply the coercion rules. Returns ``(arguments, used_alias)``.

    The input dict is returned as-is (same object) when no rule applies.
    ``used_alias`` is true when a parameter was renamed from an alias.
    ``droppable_none`` comes from :func:`optional_non_nullable`.
    """
    if not isinstance(arguments, dict):
        return {}, False
    name = canonical_tool((tool or "").strip())
    args = arguments
    used_alias = False

    def _edit() -> dict[str, Any]:
        nonlocal args
        if args is arguments:
            args = dict(arguments)
        return args

    for key in [k for k, v in arguments.items() if v is None and k in droppable_none]:
        del _edit()[key]

    for canonical, aliases in _RENAMES.get(name, {}).items():
        present = [a for a in aliases if args.get(a) is not None]
        if args.get(canonical) is not None or len(present) != 1:
            continue
        edited = _edit()
        edited[canonical] = edited.pop(present[0])
        used_alias = True

    if name == "patch_note" and "ops" in args:
        ops = _parse_json(args["ops"], (list, dict))
        if isinstance(ops, dict):
            ops = [ops]
        if ops is not args["ops"]:
            _edit()["ops"] = ops

    for key in _JSON_OBJECT_PARAMS.get(name, ()):
        if key in args:
            parsed = _parse_json(args[key], (dict,))
            if parsed is not args[key]:
                _edit()[key] = parsed

    return args, used_alias
