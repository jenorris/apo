"""Typed patch_note ops for MCP schema (discriminated union on ``op``).

Hosts that understand ``oneOf`` + ``discriminator`` get per-op required keys;
others still see the Field description on the ``ops`` list.

Role vocabulary (agent UX):
- **target** — required identity of a section mutation (``replace_section``;
  optional location for ``append`` / ``prepend``). Canonical wire key remains
  ``heading``; ``target`` is an accepted alias.
- **scope** — optional search bound for find/replace-style ops
  (``replace_text``). ``scope.heading`` is canonical; top-level ``heading``
  is an accepted alias (agent success rate).
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
    chunk_hash: str | None = None


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
    # Alias for scope.chunk_hash — search hit → scoped replace without re-read.
    chunk_hash: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _alias_heading_to_scope(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        top = data.get("heading")
        top_ch = data.get("chunk_hash")
        scope = data.get("scope")
        if top is None and top_ch is None:
            return data
        if scope is None:
            scope = {}
        elif not isinstance(scope, dict):
            return data
        scope = dict(scope)
        if top is not None:
            sh = scope.get("heading")
            if sh is not None:
                _conflict(str(top), str(sh), left="heading", right="scope.heading")
            scope["heading"] = top
        if top_ch is not None:
            sch = scope.get("chunk_hash")
            if sch is not None:
                _conflict(str(top_ch), str(sch), left="chunk_hash", right="scope.chunk_hash")
            scope["chunk_hash"] = top_ch
        return {**data, "scope": scope}


class ReplaceSectionOp(_OpBase):
    op: Literal["replace_section"]
    heading: str | None = None
    text: str = ""
    # Alias for heading (target role).
    target: str | None = None
    chunk_hash: str | None = None

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

    @model_validator(mode="after")
    def _require_anchor(self) -> ReplaceSectionOp:
        if not (self.heading or self.chunk_hash):
            raise ValueError(
                "replace_section requires heading|target or chunk_hash"
            )
        return self


class AppendOp(_OpBase):
    op: Literal["append"]
    text: str
    heading: str | None = None
    target: str | None = None
    chunk_hash: str | None = None
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
    chunk_hash: str | None = None
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


PatchOp = Annotated[
    Union[
        SetFieldOp,
        DeleteFieldOp,
        ReplaceTextOp,
        ReplaceSectionOp,
        AppendOp,
        PrependOp,
        AppendEofOp,
    ],
    Field(discriminator="op"),
]

OPS_FIELD_DESC = (
    "Deterministic mutators; discriminated by op. "
    "Keys: field/find/replace — never key/old/new. "
    "Ops: set_field(field,value); delete_field(field); "
    "replace_text(find,replace,scope.heading|heading|chunk_hash); "
    "replace_section(heading|target|chunk_hash,text); "
    "append/prepend(text,heading|target|chunk_hash); append_eof(text). "
    "Standalone add → append_note. "
    "Aliases frozen: target≡heading; replace_text heading≡scope.heading; "
    "chunk_hash≡search hit (stale → heading fallback when path+heading known)."
)

PATCH_NOTES_ITEMS_DESC = (
    "Multi-path mode for patch_note: same-vault batch. "
    "Each item: path + ops (+ optional expected_mtime). "
    "Max 20 items; duplicate paths rejected. Partial failures continue; check per-item ok. "
    "XOR with path+ops — do not pass both. "
    "Cross-role parallel writes (e.g. two different notes) stay separate MCP calls."
)


class PatchNotesItem(BaseModel):
    """One path in a ``patch_notes`` batch."""

    model_config = ConfigDict(extra="forbid")

    path: str
    ops: Annotated[list[PatchOp], Field(description=OPS_FIELD_DESC)]
    expected_mtime: float | None = Field(
        default=None,
        description=(
            "Optimistic concurrency for this path: pass mtime from a prior read/write. "
            "On stale_write, re-read and retry that item."
        ),
    )


def normalize_op_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize aliases for the markdown apply path (dict or model dump).

    - replace_text: top-level ``heading`` → ``scope.heading``;
      top-level ``chunk_hash`` → ``scope.chunk_hash`` (resolved later in ops)
    - replace_section / append / prepend: ``target`` → ``heading``
    Alias keys are stripped so apply_op sees one canonical shape.
    ``chunk_hash`` is kept until ``materialize_ops_chunk_hashes``.
    """
    data = dict(data)
    kind = data.get("op")

    if kind == "replace_text":
        top = data.pop("heading", None)
        top_ch = data.pop("chunk_hash", None)
        scope_raw = data.get("scope")
        scope: dict[str, Any] = dict(scope_raw) if isinstance(scope_raw, dict) else {}
        sh = scope.get("heading")
        if top is not None and sh is not None:
            _conflict(str(top), str(sh), left="heading", right="scope.heading")
        if top is not None:
            scope["heading"] = top
        sch = scope.get("chunk_hash")
        if top_ch is not None and sch is not None:
            _conflict(str(top_ch), str(sch), left="chunk_hash", right="scope.chunk_hash")
        if top_ch is not None:
            scope["chunk_hash"] = top_ch
        cleaned: dict[str, Any] = {}
        if scope.get("heading") is not None:
            cleaned["heading"] = scope["heading"]
        if scope.get("chunk_hash") is not None:
            cleaned["chunk_hash"] = scope["chunk_hash"]
        if cleaned:
            data["scope"] = cleaned
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
