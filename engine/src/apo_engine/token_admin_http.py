"""``/admin/clients``: the HTTP face of :class:`apo_engine.token_admin.TokenRegistry`.

Only on a token-auth instance (``APO_MCP_AUTH=token``), and only for admin
clients (an ``admin`` policy in the tokens file). Every route authenticates
the bearer itself: custom routes sit outside FastMCP's /mcp auth.

    GET    /admin/clients                  the clients this admin manages (no digests)
    POST   /admin/clients                  {name, vaults, default_vault?} -> 201 {…, token}
    PUT    /admin/clients/{name}           {vaults, default_vault?}       -> rescope
    POST   /admin/clients/{name}/rotate                                   -> {…, token}
    DELETE /admin/clients/{name}                                          -> 204

A token appears in exactly one response, the one that created it. Logs name
the admin, the action and the client, never a token.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from apo_engine.token_admin import AdminError, TokenRegistry

logger = logging.getLogger(__name__)


def register_admin_routes(mcp, verifier, tokens_path: str, known_vaults: Callable[[], Iterable[str]]) -> None:
    registry = TokenRegistry(tokens_path, known_vaults)

    def admin_of(request: Request):
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise AdminError("admin bearer token required", 401)
        found = verifier.verify_admin(header[7:].strip())
        if found is None:
            raise AdminError("not an admin token", 401)
        return found

    async def body(request: Request) -> dict:
        try:
            data = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError as exc:
            raise AdminError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise AdminError("body must be a JSON object")
        return data

    def guarded(action: str):
        def wrap(fn):
            async def handler(request: Request) -> Response:
                try:
                    actor, policy = admin_of(request)
                    response = await fn(request, actor, policy)
                    logger.info("token admin: %s %s %s", actor, action, request.path_params.get("name", ""))
                    return response
                except AdminError as exc:
                    if exc.status in (401, 403):
                        logger.warning("token admin refused (%s): %s", action, exc)
                    return JSONResponse({"error": str(exc)}, status_code=exc.status)

            return handler

        return wrap

    @mcp.custom_route("/admin/clients", methods=["GET"], include_in_schema=False)
    @guarded("list")
    async def list_clients(request, actor, policy):
        return JSONResponse({"clients": registry.list(policy)})

    @mcp.custom_route("/admin/clients", methods=["POST"], include_in_schema=False)
    @guarded("issue")
    async def issue_client(request, actor, policy):
        b = await body(request)
        out = registry.issue(policy, actor, b.get("name"), b.get("vaults"), b.get("default_vault"))
        return JSONResponse(out, status_code=201, headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/admin/clients/{name}", methods=["PUT"], include_in_schema=False)
    @guarded("rescope")
    async def rescope_client(request, actor, policy):
        b = await body(request)
        return JSONResponse(registry.rescope(policy, actor, request.path_params["name"], b.get("vaults"), b.get("default_vault")))

    @mcp.custom_route("/admin/clients/{name}/rotate", methods=["POST"], include_in_schema=False)
    @guarded("rotate")
    async def rotate_client(request, actor, policy):
        return JSONResponse(registry.rotate(policy, actor, request.path_params["name"]), headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/admin/clients/{name}", methods=["DELETE"], include_in_schema=False)
    @guarded("revoke")
    async def revoke_client(request, actor, policy):
        registry.revoke(policy, actor, request.path_params["name"])
        return Response(status_code=204)
