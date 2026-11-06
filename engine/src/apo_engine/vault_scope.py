"""FastMCP middleware: enforce a token client's vault scope server-side.

A bearer client whose tokens-file entry lists ``vaults`` (see
:class:`apo_engine.mcp_auth.StaticBearerTokenVerifier`) can only touch those
vaults, whatever its prompt says:

- every vault a request names — ``vault=``, ``vaults=[]``, a ``<vault_id>:``
  prefix on ``path`` / ``folder`` / ``folders``, the ``vault`` tool's nested
  ``request`` (``vault`` / ``vaults`` / ``to``), ``note://<vault>/…`` resources
  — must be in scope, else the call fails with ``vault_out_of_scope``;
- a request that names no vault gets the client's ``default_vault`` (or, for
  multi-vault ``vault`` actions, its whole scope) instead of the server default;
- ``apo_admin`` and any tool this middleware doesn't know are refused, and
  hidden from ``tools/list``;
- ``memory://vaults`` lists only in-scope vaults.

Unscoped callers (no token, or a token without ``vaults``) pass through
untouched, so stdio, the loopback instance and existing clients are unaffected.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

# Tools that take a top-level ``vault=`` (and maybe ``vaults=``/prefixed paths).
VAULT_ARG_TOOLS = frozenset({
    "write_note", "append_note", "patch_note", "patch_table", "read_note",
    "search_notes", "graph_neighbors", "filter_notes", "backlinks", "history",
})
# ``vault`` tool actions whose request takes ``vaults=[...]`` rather than one vault.
MULTI_VAULT_ACTIONS = frozenset({"list", "merge", "project"})
PREFIXABLE_ARGS = ("path", "folder")
DENIED_TOOLS = frozenset({"apo_admin"})


def _scope() -> tuple[list[str], str] | None:
    token = get_access_token()
    claims = (token.claims or {}) if token is not None else {}
    vaults = claims.get("apo_vaults")
    if not vaults:
        return None
    return list(vaults), claims.get("apo_default_vault") or vaults[0]


class VaultScopeMiddleware(Middleware):
    def __init__(self, known_vaults: Callable[[], Iterable[str]]):
        # Callable so registry reloads are seen; used to spot "<vault>:" prefixes.
        self._known_vaults = known_vaults

    # -- helpers -------------------------------------------------------------

    def _prefixed_vault(self, value: Any) -> str | None:
        if not isinstance(value, str) or ":" not in value:
            return None
        head = value.split(":", 1)[0].strip()
        return head if head in set(self._known_vaults()) else None

    @staticmethod
    def _deny(names: Iterable[str], allowed: list[str]) -> None:
        bad = sorted({n for n in names if n and n not in allowed})
        if bad:
            raise ToolError(
                f"vault_out_of_scope: this client may only use {', '.join(allowed)}; "
                f"refused {', '.join(bad)}"
            )

    def _referenced(self, args: dict[str, Any]) -> set[str]:
        named: set[str] = set()
        if isinstance(args.get("vault"), str) and args["vault"].strip():
            named.add(args["vault"].strip())
        for key in ("vaults", "to"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                named.add(val.strip())
            elif isinstance(val, list):
                named.update(v.strip() for v in val if isinstance(v, str) and v.strip())
        for key in PREFIXABLE_ARGS:
            if (v := self._prefixed_vault(args.get(key))) is not None:
                named.add(v)
        for f in args.get("folders") or []:
            if (v := self._prefixed_vault(f)) is not None:
                named.add(v)
        return named

    # -- hooks ---------------------------------------------------------------

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext[Any, Any]) -> Any:
        scope = _scope()
        if scope is None:
            return await call_next(context)
        allowed, default = scope
        name = context.message.name
        args = dict(context.message.arguments or {})

        if name in DENIED_TOOLS:
            raise ToolError(f"vault_out_of_scope: {name} is not available to vault-scoped clients")

        if name == "vault":
            request = dict(args.get("request") or {})
            named = self._referenced(request)
            self._deny(named, allowed)
            if not named:
                action = str(request.get("action") or "list").strip().lower()
                if action in MULTI_VAULT_ACTIONS or action in ("contracts", "describe"):
                    request["vaults"] = list(allowed)
                else:
                    request["vault"] = default
            args["request"] = request
        elif name in VAULT_ARG_TOOLS:
            named = self._referenced(args)
            self._deny(named, allowed)
            if not named:
                args["vault"] = default
        else:
            raise ToolError(f"vault_out_of_scope: {name} is not available to vault-scoped clients")

        message = context.message.model_copy(update={"arguments": args})
        return await call_next(context.copy(message=message))

    async def on_list_tools(self, context: MiddlewareContext, call_next: CallNext[Any, Any]) -> Any:
        tools = await call_next(context)
        if _scope() is None:
            return tools
        return [t for t in tools if t.name in VAULT_ARG_TOOLS or t.name == "vault"]

    async def on_read_resource(self, context: MiddlewareContext, call_next: CallNext[Any, Any]) -> Any:
        scope = _scope()
        if scope is None:
            return await call_next(context)
        allowed, _ = scope
        uri = str(context.message.uri)
        if uri.startswith("note://"):
            vault = uri[len("note://"):].split("/", 1)[0]
            if vault not in allowed:
                raise ResourceError(f"vault_out_of_scope: {vault}")
            return await call_next(context)
        if uri == "memory://vaults":
            return await call_next(context)  # filtered by the resource itself
        raise ResourceError(f"vault_out_of_scope: {uri} is not available to vault-scoped clients")


def scoped_vault_ids(all_ids: Iterable[str]) -> list[str]:
    """For resources: the caller's in-scope subset of ``all_ids`` (all when unscoped)."""
    scope = _scope()
    ids = list(all_ids)
    return ids if scope is None else [v for v in ids if v in scope[0]]
