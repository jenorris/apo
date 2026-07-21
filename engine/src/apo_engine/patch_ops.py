"""Typed patch_note ops for MCP schema (discriminated union on ``op``).

Hosts that understand ``oneOf`` + ``discriminator`` get per-op required keys;
others still see the Field description on the ``ops`` list.

Role vocabulary (agent UX):
- **target** — required identity of a section mutation (``replace_section``;
  optional location for ``append`` / ``prepend``). Canonical wire key remains
  ``heading``; ``target`` is an accepted alias.
- **scope** — optional search bound for find/replace-style ops
  (``replace_text``, ``check_item``). ``scope.heading`` is canonical;
  top-level ``heading`` is an accepted alias (agent success rate).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _OpBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetFieldOp(_OpBase):
    op: Literal["set_field"]
    field: str
    value: Any = ""


class DeleteFieldOp(_OpBase):
    op: Literal["delete_field"]
    field: str


class ReplaceTextScope(_OpBase):
    heading: str | None = None


def _conflict(a: str, b: str, *, left: str, right: str) -> None:
    if a != b:
        raise ValueError(f"conflicting {left} and {right}: {a!r} vs {b!r}")


class ReplaceTextOp(_OpBase):
    op: Literal["replace_text"]
    find: str
    replace: str = ""
    count: int = 1
    scope: ReplaceTextScope | None = None
    # Alias for scope.heading — agents often flatten "heading" to the top level.
    heading: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _alias_heading_to_scope(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        top = data.get("heading")
        if top is None:
            return data
        scope = data.get("scope")
        if scope is None:
            return {**data, "scope": {"heading": top}}
        if isinstance(scope, dict):
            sh = scope.get("heading")
            if sh is not None:
                _conflict(str(top), str(sh), left="heading", right="scope.heading")
            return {**data, "scope": {**scope, "heading": top}}
        return data


class ReplaceSectionOp(_OpBase):
    op: Literal["replace_section"]
    heading: str
    text: str = ""
    # Alias for heading (target role).
    target: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _alias_target_to_heading(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        target = data.get("target")
        if target is None:
            return data
        heading = data.get("heading")
        if heading is not None:
            _conflict(str(target), str(heading), left="target", right="heading")
        return {**data, "heading": target}


class AppendOp(_OpBase):
    op: Literal["append"]
    text: str
    heading: str | None = None
    target: str | None = None
    position: Literal["start", "end"] = "end"

    @model_validator(mode="before")
    @classmethod
    def _alias_target_to_heading(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        target = data.get("target")
        if target is None:
            return data
        heading = data.get("heading")
        if heading is not None:
            _conflict(str(target), str(heading), left="target", right="heading")
        return {**data, "heading": target}


class PrependOp(_OpBase):
    op: Literal["prepend"]
    text: str
    heading: str | None = None
    target: str | None = None
    position: Literal["start", "end"] = "start"

    @model_validator(mode="before")
    @classmethod
    def _alias_target_to_heading(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        target = data.get("target")
        if target is None:
            return data
        heading = data.get("heading")
        if heading is not None:
            _conflict(str(target), str(heading), left="target", right="heading")
        return {**data, "heading": target}


class AppendEofOp(_OpBase):
    op: Literal["append_eof"]
    text: str


class CheckItemOp(_OpBase):
    """Flip a markdown checkbox line (intent op — prefer over scoped replace_text)."""

    op: Literal["check_item"]
    item: str
    checked: bool = True
    count: int = 1
    scope: ReplaceTextScope | None = None
    heading: str | None = None  # alias for scope.heading

    @model_validator(mode="before")
    @classmethod
    def _alias_heading_to_scope(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        top = data.get("heading")
        if top is None:
            return data
        scope = data.get("scope")
        if scope is None:
            return {**data, "scope": {"heading": top}}
        if isinstance(scope, dict):
            sh = scope.get("heading")
            if sh is not None:
                _conflict(str(top), str(sh), left="heading", right="scope.heading")
            return {**data, "scope": {**scope, "heading": top}}
        return data


PatchOp = Annotated[
    Union[
        SetFieldOp,
        DeleteFieldOp,
        ReplaceTextOp,
        ReplaceSectionOp,
        AppendOp,
        PrependOp,
        AppendEofOp,
        CheckItemOp,
    ],
    Field(discriminator="op"),
]

OPS_FIELD_DESC = (
    "Deterministic mutators; discriminated by op. "
    "Keys are field/find/replace — never key/old/new. "
    "Roles: target (section identity; wire key `heading`, alias `target`) vs "
    "scope (search bound; `scope.heading`, alias top-level `heading` on "
    "replace_text/check_item). Prefer check_item for checkbox flips."
)


def normalize_op_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize aliases for the markdown apply path (dict or model dump).

    - replace_text / check_item: top-level ``heading`` → ``scope.heading``
    - replace_section / append / prepend: ``target`` → ``heading``
    Alias keys are stripped so apply_op sees one canonical shape.
    """
    data = dict(data)
    kind = data.get("op")

    if kind in ("replace_text", "check_item"):
        top = data.pop("heading", None)
        scope_raw = data.get("scope")
        scope: dict[str, Any] = dict(scope_raw) if isinstance(scope_raw, dict) else {}
        sh = scope.get("heading")
        if top is not None and sh is not None:
            _conflict(str(top), str(sh), left="heading", right="scope.heading")
        if top is not None:
            scope["heading"] = top
        if scope.get("heading") is not None:
            data["scope"] = {"heading": scope["heading"]}
        else:
            data.pop("scope", None)
        data.pop("target", None)

    elif kind in ("replace_section", "append", "prepend"):
        target = data.pop("target", None)
        if target is not None:
            heading = data.get("heading")
            if heading is not None:
                _conflict(str(target), str(heading), left="target", right="heading")
            data["heading"] = target

    return data


def ops_to_dicts(ops: list[Any]) -> list[dict[str, Any]]:
    """Normalize MCP-validated models (or plain dicts) for ``apply_patch``."""
    out: list[dict[str, Any]] = []
    for op in ops:
        if isinstance(op, BaseModel):
            data = op.model_dump(mode="python", exclude_none=True)
        elif isinstance(op, dict):
            data = dict(op)
        else:
            raise TypeError(f"unsupported patch op type: {type(op)!r}")
        out.append(normalize_op_dict(data))
    return out
