"""Local JSON HTTP RPC for apo-engine (loopback / optional Unix socket).

Intended clients: apo-enterprise Laravel gateway (and any non-stdio host).
Auth: optional shared bearer token (APO_RPC_TOKEN). Bind defaults to 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from apo_engine import ops

Handler = Callable[[dict[str, Any]], dict[str, Any]]

_ROUTES: dict[tuple[str, str], Handler] = {}


def _route(method: str, path: str):
    def deco(fn: Handler) -> Handler:
        _ROUTES[(method.upper(), path)] = fn
        return fn

    return deco


@_route("GET", "/health")
def _health(_body: dict[str, Any]) -> dict[str, Any]:
    return ops.health()


@_route("GET", "/v1/stats")
@_route("POST", "/v1/stats")
def _stats(body: dict[str, Any]) -> dict[str, Any]:
    return ops.stats(vault=str(body.get("vault") or ""))


@_route("POST", "/v1/search")
def _search(body: dict[str, Any]) -> dict[str, Any]:
    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"ok": False, "error": "bad_request", "message": "`query` string required"}
    exclude = body.get("exclude")
    if exclude is not None and not isinstance(exclude, list):
        return {"ok": False, "error": "bad_request", "message": "`exclude` must be an array of strings"}
    return ops.search(
        query,
        top_k=int(body.get("top_k") or body.get("k") or 5),
        folder=str(body.get("folder") or ""),
        vault=str(body.get("vault") or ""),
        snippet_chars=int(body.get("snippet_chars", 240)),
        exclude=[str(x) for x in exclude] if exclude else None,
        hybrid=not bool(body.get("no_hybrid")),
    )


@_route("POST", "/v1/read")
def _read(body: dict[str, Any]) -> dict[str, Any]:
    path = body.get("path")
    if not isinstance(path, str) or not path.strip():
        return {"ok": False, "error": "bad_request", "message": "`path` string required"}
    heading = body.get("heading")
    if heading is not None and not isinstance(heading, str):
        return {"ok": False, "error": "bad_request", "message": "`heading` must be a string"}
    return ops.read_note(path, heading=heading, vault=str(body.get("vault") or ""))


@_route("POST", "/v1/filter")
def _filter(body: dict[str, Any]) -> dict[str, Any]:
    where = body.get("where", {})
    if where is not None and not isinstance(where, dict):
        return {
            "ok": False,
            "error": "bad_query",
            "message": "`where` must be an object (use {} to list all indexed notes in folder)",
        }
    return ops.filter_notes(
        where or {},
        folder=str(body.get("folder") or ""),
        limit=int(body.get("limit") or 20),
        vault=str(body.get("vault") or ""),
    )


@_route("POST", "/v1/expand")
def _expand(body: dict[str, Any]) -> dict[str, Any]:
    chunk_hash = body.get("chunk_hash")
    if not isinstance(chunk_hash, str) or not chunk_hash.strip():
        return {"ok": False, "error": "bad_request", "message": "`chunk_hash` string required"}
    scope = str(body.get("scope") or "section")
    if scope not in ("section", "chunk"):
        return {"ok": False, "error": "bad_request", "message": "`scope` must be section|chunk"}
    return ops.expand_chunk(
        chunk_hash,
        vault=str(body.get("vault") or ""),
        scope=scope,  # type: ignore[arg-type]
    )


@_route("POST", "/v1/backlinks")
def _backlinks(body: dict[str, Any]) -> dict[str, Any]:
    path = body.get("path")
    if not isinstance(path, str) or not path.strip():
        return {"ok": False, "error": "bad_request", "message": "`path` string required"}
    return ops.backlinks(
        path,
        limit=int(body.get("limit") or 100),
        vault=str(body.get("vault") or ""),
    )


@_route("POST", "/v1/recent")
def _recent(body: dict[str, Any]) -> dict[str, Any]:
    return ops.recent_activity(
        limit=int(body.get("limit") or 10),
        folder=str(body.get("folder") or ""),
        vault=str(body.get("vault") or ""),
    )


def _opt_float(body: dict[str, Any], key: str) -> float | None:
    if key not in body or body[key] is None:
        return None
    return float(body[key])


@_route("POST", "/v1/write")
def _write(body: dict[str, Any]) -> dict[str, Any]:
    path = body.get("path")
    content = body.get("content")
    if not isinstance(path, str) or not path.strip():
        return {"ok": False, "error": "bad_request", "message": "`path` string required"}
    if not isinstance(content, str):
        return {"ok": False, "error": "bad_request", "message": "`content` string required"}
    return ops.write_note(
        path,
        content,
        append=bool(body.get("append")),
        expected_mtime=_opt_float(body, "expected_mtime"),
        vault=str(body.get("vault") or ""),
    )


@_route("POST", "/v1/append")
def _append(body: dict[str, Any]) -> dict[str, Any]:
    path = body.get("path")
    text = body.get("text")
    if not isinstance(path, str) or not path.strip():
        return {"ok": False, "error": "bad_request", "message": "`path` string required"}
    if not isinstance(text, str):
        return {"ok": False, "error": "bad_request", "message": "`text` string required"}
    position = str(body.get("position") or "end")
    if position not in ("end", "start"):
        return {"ok": False, "error": "bad_request", "message": "`position` must be end|start"}
    heading = body.get("heading")
    chunk_hash = body.get("chunk_hash")
    if heading is not None and not isinstance(heading, str):
        return {"ok": False, "error": "bad_request", "message": "`heading` must be a string"}
    if chunk_hash is not None and not isinstance(chunk_hash, str):
        return {"ok": False, "error": "bad_request", "message": "`chunk_hash` must be a string"}
    return ops.append_note(
        path,
        text,
        heading=heading,
        chunk_hash=chunk_hash,
        position=position,  # type: ignore[arg-type]
        create=bool(body.get("create")),
        expected_mtime=_opt_float(body, "expected_mtime"),
        vault=str(body.get("vault") or ""),
    )


@_route("POST", "/v1/patch")
def _patch(body: dict[str, Any]) -> dict[str, Any]:
    path = body.get("path")
    patch_ops = body.get("ops")
    if not isinstance(path, str) or not path.strip():
        return {"ok": False, "error": "bad_request", "message": "`path` string required"}
    if not isinstance(patch_ops, list):
        return {"ok": False, "error": "bad_request", "message": "`ops` must be an array"}
    return ops.patch_note(
        path,
        patch_ops,
        strict=bool(body.get("strict")),
        dry_run=bool(body.get("dry_run")),
        verbose=bool(body.get("verbose")),
        expected_mtime=_opt_float(body, "expected_mtime"),
        vault=str(body.get("vault") or ""),
    )


@_route("POST", "/v1/move")
def _move(body: dict[str, Any]) -> dict[str, Any]:
    src = body.get("src")
    dst = body.get("dst")
    if not isinstance(src, str) or not src.strip():
        return {"ok": False, "error": "bad_request", "message": "`src` string required"}
    if not isinstance(dst, str) or not dst.strip():
        return {"ok": False, "error": "bad_request", "message": "`dst` string required"}
    return ops.move_note(
        src,
        dst,
        overwrite=bool(body.get("overwrite")),
        vault=str(body.get("vault") or ""),
    )


@_route("POST", "/v1/delete")
def _delete(body: dict[str, Any]) -> dict[str, Any]:
    path = body.get("path")
    if not isinstance(path, str) or not path.strip():
        return {"ok": False, "error": "bad_request", "message": "`path` string required"}
    return ops.delete_note(path, vault=str(body.get("vault") or ""))


def _json_bytes(payload: dict[str, Any], status: int) -> tuple[bytes, int]:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8"), status


class RpcHandler(BaseHTTPRequestHandler):
    server_version = "apo-engine-rpc/0.1"
    rpc_token: str = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _unauthorized(self) -> None:
        body, _ = _json_bytes(
            {"ok": False, "error": "unauthorized", "message": "invalid or missing bearer token"},
            401,
        )
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        token = (self.rpc_token or "").strip()
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:].strip() == token:
            return True
        if self.headers.get("X-Apo-Token", "").strip() == token:
            return True
        return False

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"invalid JSON body: {e}") from e
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _dispatch(self, method: str) -> None:
        if not self._check_auth():
            self._unauthorized()
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path != "/health" and not path.startswith("/v1"):
            # normalize trailing slash variants already handled
            pass
        # Allow /health and /v1/... without requiring trailing slash
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        handler = _ROUTES.get((method, path))
        if handler is None and method == "GET" and path == "/":
            handler = _health
            body: dict[str, Any] = {}
        elif handler is None:
            payload, status = _json_bytes(
                {"ok": False, "error": "not_found", "message": f"no route {method} {path}"},
                404,
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        else:
            try:
                body = self._read_json() if method in ("POST", "PUT", "PATCH") else {}
            except ValueError as e:
                payload, status = _json_bytes(
                    {"ok": False, "error": "bad_request", "message": str(e)},
                    400,
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

        try:
            result = handler(body)
        except Exception as e:
            result = {"ok": False, "error": "internal", "message": str(e)}

        status = 200 if result.get("ok") else 400
        err = result.get("error")
        if err == "unauthorized":
            status = 401
        elif err in ("not_found", "anchor_not_found"):
            status = 404
        elif err in ("stale_write", "destination_exists", "path_mismatch"):
            status = 409
        payload, _ = _json_bytes(result, status)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")


class _UnixThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX


def run_rpc(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    socket_path: str | None = None,
    token: str | None = None,
) -> None:
    """Block serving until killed. Prefer loopback TCP; optional Unix domain socket."""
    RpcHandler.rpc_token = (token if token is not None else os.environ.get("APO_RPC_TOKEN", "")).strip()

    if socket_path:
        path = Path(socket_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        server: ThreadingHTTPServer = _UnixThreadingHTTPServer(str(path), RpcHandler)
        bind_desc = f"unix:{path}"
    else:
        server = ThreadingHTTPServer((host, port), RpcHandler)
        bind_desc = f"http://{host}:{port}"

    sys.stderr.write(f"apo-engine rpc listening on {bind_desc}\n")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if socket_path:
            try:
                Path(socket_path).expanduser().unlink(missing_ok=True)
            except OSError:
                pass
