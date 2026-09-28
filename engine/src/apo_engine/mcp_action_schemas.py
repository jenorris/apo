"""Typed per-action request models for the ``vault`` MCP tool (discriminated
union on ``action``), mirroring ``patch_ops.py``'s op-union pattern for note
mutations.

Before: one flat parameter bag per tool, most fields scoped only by prose
("lint only:", "clone only:") — e.g. ``vault``'s 12 params where any single
action actually reads 2-6 of them. After: each action gets its own request
model with only the fields it consumes (see ``apo_engine.ops.vault_op`` for
what each action actually reads), and FastMCP/pydantic reject unknown fields
per variant instead of silently accepting (and ignoring) a field that belongs
to a different action.

Field descriptions here are deliberately terse: a discriminated union repeats
each variant's own field schema in full (no shared "lint only:" prose block
to say it once), so verbose per-field text multiplies by variant count and
the whole point — fixing the interface without inflating every session's
tool-list cost — backfires. See test_mcp_schema_size.py's char ceiling.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class _ActionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VaultListRequest(_ActionBase):
    action: Literal["list"]
    vaults: list[str] | None = None


class VaultContractsRequest(_ActionBase):
    action: Literal["contracts"]
    vault: str = ""
    vaults: list[str] | None = None
    full: bool = False


class VaultDescribeRequest(_ActionBase):
    action: Literal["describe"]
    vault: str = ""
    vaults: list[str] | None = None
    full: bool = False


class VaultMergeRequest(_ActionBase):
    action: Literal["merge"]
    vaults: list[str] | None = None
    full: bool = False


class VaultProjectRequest(_ActionBase):
    action: Literal["project"]
    vaults: list[str] | None = None
    mode: Annotated[
        str,
        Field(description="full (default): complete body. index: compact pointer surface."),
    ] = "full"


class VaultStatsRequest(_ActionBase):
    action: Literal["stats"]
    vault: str = ""
    days: int | None = 7


class VaultLintRequest(_ActionBase):
    action: Literal["lint"]
    vault: str = ""
    folder: str = ""
    limit: int | None = 50
    offset: int = 0
    fix: Annotated[bool, Field(description="Trailing-whitespace auto-fix.")] = False
    known_skills: list[str] | None = None


class VaultCloneRequest(_ActionBase):
    action: Literal["clone"]
    vault: str = ""
    to: Annotated[str, Field(description="Destination vault id (must exist).")] = ""
    dry_run: bool = False


class VaultOkfDryRunRequest(_ActionBase):
    action: Literal["okf_dry_run"]
    vault: str = ""
    contract: Annotated[str, Field(description="Proposed okf-contract.schema.yaml text.")] = ""


VaultAction = Annotated[
    Union[
        VaultListRequest,
        VaultContractsRequest,
        VaultDescribeRequest,
        VaultMergeRequest,
        VaultProjectRequest,
        VaultStatsRequest,
        VaultLintRequest,
        VaultCloneRequest,
        VaultOkfDryRunRequest,
    ],
    Field(discriminator="action"),
]

VAULT_ACTION_FIELD_DESC = (
    "list | contracts | describe | merge | project | stats | lint | clone | okf_dry_run — "
    "each variant lists only its own params."
)
