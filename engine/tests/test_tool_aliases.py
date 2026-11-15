"""CLI-shaped MCP tool aliases resolve to the same callable as the canonical tool."""

from __future__ import annotations

import asyncio
import json
from unittest import mock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken

from apo_engine import vault_scope
from apo_engine.tool_aliases import ALIAS_TAG, TOOL_ALIASES, canonical_tool
from apo_engine.tool_aliases_middleware import AliasListingMiddleware
from apo_engine.validation_hints import format_tool_validation_error
from apo_engine.vault_scope import VaultScopeMiddleware

from test_patch_note_schema import _list_tools_lean

EXPECTED = {
    "read": "read_note",
    "search": "search_notes",
    "write": "write_note",
    "append": "append_note",
    "patch": "patch_note",
    "filter": "filter_notes",
}
CANONICAL = {
    "write_note", "append_note", "patch_note", "patch_table", "read_note",
    "search_notes", "graph_neighbors", "filter_notes", "backlinks", "history",
    "vault", "apo_admin",
}


@pytest.fixture(scope="module")
def server_mod():
    mod, _tools = _list_tools_lean(collection="tool_alias_test")
    return mod


def _registry(mod) -> dict:
    tools = asyncio.run(mod.mcp.list_tools(run_middleware=False))
    return {t.name: t for t in tools}


def test_alias_map_is_the_documented_one() -> None:
    assert TOOL_ALIASES == EXPECTED
    assert canonical_tool("search") == "search_notes"
    assert canonical_tool("search_notes") == "search_notes"
    assert canonical_tool("history") == "history"


@pytest.mark.parametrize(("alias", "canonical"), sorted(EXPECTED.items()))
def test_alias_and_canonical_resolve_to_same_callable(server_mod, alias, canonical) -> None:
    reg = _registry(server_mod)
    assert reg[alias].fn is reg[canonical].fn
    assert reg[alias].fn is getattr(server_mod, canonical)
    assert reg[alias].parameters == reg[canonical].parameters
    assert reg[alias].annotations == reg[canonical].annotations
    assert reg[alias].output_schema == reg[canonical].output_schema
    assert ALIAS_TAG in reg[alias].tags
    assert ALIAS_TAG not in (reg[canonical].tags or ())


def test_canonical_tools_unchanged_and_aliases_not_counted(server_mod) -> None:
    reg = _registry(server_mod)
    assert {n for n, t in reg.items() if ALIAS_TAG not in (t.tags or ())} == CANONICAL
    assert {n for n, t in reg.items() if ALIAS_TAG in (t.tags or ())} == set(EXPECTED)
    listed = asyncio.run(server_mod.mcp.list_tools())
    assert {t.name for t in listed} == CANONICAL


def test_aliases_listed_when_opted_in(server_mod, monkeypatch) -> None:
    monkeypatch.setenv("APO_MCP_LIST_ALIASES", "1")
    listed = asyncio.run(server_mod.mcp.list_tools())
    assert {t.name for t in listed} == CANONICAL | set(EXPECTED)


def test_alias_is_callable_and_matches_canonical_result(server_mod) -> None:
    async def go(name):
        async with Client(server_mod.mcp) as c:
            r = await c.call_tool(name, {"query": "x", "vault": "nope"}, raise_on_error=False)
            return r.is_error, [getattr(b, "text", None) for b in r.content]

    assert asyncio.run(go("search")) == asyncio.run(go("search_notes"))


def _scoped_server() -> FastMCP:
    m = FastMCP("alias-scope-test")

    def search_notes(query: str, vault: str = "") -> dict:
        return {"vault": vault}

    m.tool(search_notes)
    m.tool(search_notes, name="search", tags={ALIAS_TAG})

    m.add_middleware(AliasListingMiddleware())
    m.add_middleware(VaultScopeMiddleware(lambda: ["atlas", "lyra"]))
    return m


def test_scoped_client_gets_default_vault_through_alias() -> None:
    token = AccessToken(
        token="t", client_id="c", scopes=[],
        claims={"apo_vaults": ["atlas"], "apo_default_vault": "atlas"},
    )

    async def call(name, args):
        with mock.patch.object(vault_scope, "get_access_token", return_value=token):
            async with Client(_scoped_server()) as c:
                r = await c.call_tool(name, args)
                return r.data if r.data is not None else json.loads(r.content[0].text)

    assert asyncio.run(call("search", {"query": "x"}))["vault"] == "atlas"
    with pytest.raises(ToolError, match="vault_out_of_scope"):
        asyncio.run(call("search", {"query": "x", "vault": "lyra"}))


def test_validation_hints_use_canonical_name() -> None:
    from pydantic import BaseModel, ConfigDict, ValidationError

    class Args(BaseModel):
        model_config = ConfigDict(extra="forbid")
        path: str

    with pytest.raises(ValidationError) as exc:
        Args(path="a", body="b")  # type: ignore[call-arg]
    assert format_tool_validation_error("write", exc.value) == format_tool_validation_error(
        "write_note", exc.value
    )
    assert "write_note uses content=" in format_tool_validation_error("write", exc.value)
