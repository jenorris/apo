"""Typed patch_note ops for MCP schema (discriminated union on ``op``).

Hosts that understand ``oneOf`` + ``discriminator`` get per-op required keys;
others still see the Field description on the ``ops`` list.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


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


class ReplaceTextOp(_OpBase):
    op: Literal["replace_text"]
    find: str
    replace: str = ""
    count: int = 1
    scope: ReplaceTextScope | None = None


class ReplaceSectionOp(_OpBase):
    op: Literal["replace_section"]
    heading: str
    text: str = ""


class AppendOp(_OpBase):
    op: Literal["append"]
    text: str
    heading: str | None = None
    position: Literal["start", "end"] = "end"


class PrependOp(_OpBase):
    op: Literal["prepend"]
    text: str
    heading: str | None = None
    position: Literal["start", "end"] = "start"


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
    "Keys are field/find/replace — never key/old/new."
)


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
        out.append(data)
    return out
