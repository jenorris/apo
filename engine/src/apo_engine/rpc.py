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


@_route("POST", "/v1/session_stats")
def _session_stats(body: dict[str, Any]) -> dict[str, Any]:
    """Deprecated — use vault(action=stats) for habit KPIs; traces via OTel/Jaeger."""
    body = dict(body)
    body.setdefault("action", "stats")
    out = _vault(body)
    out["deprecated"] = True
    out["tip"] = "POST /v1/session_stats is deprecated; use POST /v1/vault action=stats"
    return out


@_route("POST", "/v1/telemetry")
def _telemetry(body: dict[str, Any]) -> dict[str, Any]:
    """Deprecated — habit KPIs via vault(action=stats); operator traces via otlp-mcp + Jaeger."""
    action = str(body.get("action") or "efficiency").strip().lower()
    if action in ("efficiency", "stats"):
        body = dict(body)
        body["action"] = "stats"
        out = _vault(body)
        out["deprecated"] = True
        out["tip"] = "POST /v1/telemetry action=stats is deprecated; use POST /v1/vault action=stats"
        return out
    return {
        "ok": False,
        "error": "bad_action",
        "message": (
            f"action {action!r} removed in v0.5.0 — use POST /v1/vault action=stats for habits; "
            "operator traces via otlp-mcp + Jaeger"
        ),
        "deprecated": True,
    }


@_route("POST", "/v1/search")
def _search(body: dict[str, Any]) -> dict[str, Any]:
    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"ok": False, "error": "bad_request", "message": "`query` string required"}
    exclude = body.get("exclude")
    if exclude is not None and not isinstance(exclude, list):
        return {"ok": False, "error": "bad_request", "message": "`exclude` must be an array of strings"}
    top_k = body.get("top_k", body.get("k"))
    limit = body.get("limit")
    vaults_raw = body.get("vaults")
    if vaults_raw is not None and not isinstance(vaults_raw, list):
        return {"ok": False, "error": "bad_request", "message": "`vaults` must be an array of strings"}
    vaults_arg = [str(x) for x in vaults_raw] if vaults_raw is not None else None
    folders_raw = body.get("folders")
    if folders_raw is not None and not isinstance(folders_raw, list):
        return {"ok": False, "error": "bad_request", "message": "`folders` must be an array of strings"}
    folders_arg = [str(x) for x in folders_raw] if folders_raw is not None else None
    return ops.search(
        query,
        top_k=int(top_k) if top_k is not None else None,
        limit=int(limit) if limit is not None else None,
        folder=str(body.get("folder") or ""),
        folders=folders_arg,
        vault=str(body.get("vault") or ""),
        vaults=vaults_arg,
        snippet_chars=int(body.get("snippet_chars", 240)),
        exclude=[str(x) for x in exclude] if exclude else None,
        hybrid=not bool(body.get("no_hybrid")),
        ref=str(body.get("ref") or ""),
    )


@_route("POST", "/v1/read")
def _read(body: dict[str, Any]) -> dict[str, Any]:
    path = body.get("path")
    chunk_hash = body.get("chunk_hash")
    path_s = path.strip() if isinstance(path, str) else ""
    ch_s = chunk_hash.strip() if isinstance(chunk_hash, str) else ""
    if not path_s and not ch_s:
        return {"ok": False, "error": "bad_request", "message": "`path` or `chunk_hash` required"}
    if path_s and ch_s:
        return {
            "ok": False,
            "error": "bad_request",
            "message": "pass path= or chunk_hash=, not both",
        }
    heading = body.get("heading")
    if heading is not None and not isinstance(heading, str):
        return {"ok": False, "error": "bad_request", "message": "`heading` must be a string"}
    start_line = body.get("start_line")
    end_line = body.get("end_line")
    max_chars = body.get("max_chars")
    raw = body.get("raw", False)
    if not isinstance(raw, bool):
        return {"ok": False, "error": "bad_request", "message": "`raw` must be a boolean"}
    force = bool(body.get("force"))
    fields = body.get("fields")
    if fields is not None and not isinstance(fields, list):
        return {"ok": False, "error": "bad_request", "message": "`fields` must be an array of strings"}
    if ch_s:
        return ops.read_note(
            "",
            chunk_hash=ch_s,
            vault=str(body.get("vault") or ""),
            force=force,
            fields=fields if isinstance(fields, list) else None,
        )
    return ops.read_note(
        path_s,
        heading=heading,
        vault=str(body.get("vault") or ""),
        start_line=int(start_line) if start_line is not None else None,
        end_line=int(end_line) if end_line is not None else None,
        max_chars=int(max_chars) if max_chars is not None else None,
        raw=raw,
        fields=fields if isinstance(fields, list) else None,
        lint=bool(body.get("lint")),
        ref=str(body.get("ref") or ""),
        mode=str(body.get("mode") or "auto"),
        force=bool(body.get("force")),
    )


@_route("POST", "/v1/filter")
def _filter(body: dict[str, Any]) -> dict[str, Any]:
    where = body.get("where")
    filters = body.get("filters")
    if where is None and "where" not in body and filters is None and "filters" not in body:
        where = {}
    if where is not None and not isinstance(where, dict):
        return {
            "ok": False,
            "error": "bad_query",
            "message": "`where` must be an object (use {} to list all indexed notes in folder)",
        }
    if filters is not None and not isinstance(filters, dict):
        return {
            "ok": False,
            "error": "bad_query",
            "message": "`filters` must be an object (alias for where)",
        }
    sort = body.get("sort")
    order = body.get("order")
    return ops.filter_notes(
        where,
        filters=filters,
        folder=str(body.get("folder") or ""),
        limit=int(body.get("limit") or 20),
        offset=int(body.get("offset") or 0),
        vault=str(body.get("vault") or ""),
        fields=body.get("fields") if isinstance(body.get("fields"), list) else None,
        sort=str(sort) if sort is not None else "mtime",
        order=str(order) if order is not None else "desc",
        ref=str(body.get("ref") or ""),
    )


@_route("POST", "/v1/expand")
def _expand(body: dict[str, Any]) -> dict[str, Any]:
    chunk_hash = body.get("chunk_hash")
    if not isinstance(chunk_hash, str) or not chunk_hash.strip():
        return {"ok": False, "error": "bad_request", "message": "`chunk_hash` string required"}
    force = bool(body.get("force"))
    return ops.read_note(
        "",
        chunk_hash=chunk_hash,
        vault=str(body.get("vault") or ""),
        force=force,
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


@_route("POST", "/v1/history")
def _history(body: dict[str, Any]) -> dict[str, Any]:
    """Browse by mtime or file-level git history when path= is set."""
    exclude = body.get("exclude")
    fields = body.get("fields")
    preview = body.get("preview") or "first"
    return ops.history(
        limit=int(body.get("limit") or 10),
        folder=str(body.get("folder") or ""),
        path=str(body.get("path") or ""),
        vault=str(body.get("vault") or ""),
        since=str(body.get("since") or ""),
        until=str(body.get("until") or ""),
        preview=str(preview),
        heading=str(body.get("heading") or ""),
        exclude=exclude if isinstance(exclude, list) else None,
        fields=fields if isinstance(fields, list) else None,
    )


def _opt_float(body: dict[str, Any], key: str) -> float | None:
    if key not in body or body[key] is None:
        return None
    return float(body[key])


def _opt_str(body: dict[str, Any], key: str) -> str | None:
    if key not in body or body[key] is None:
        return None
    if not isinstance(body[key], str):
        return None
    s = body[key].strip()
    return s or None


def _region_kwargs(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "expected_frontmatter_hash": _opt_str(body, "expected_frontmatter_hash"),
        "expected_body_hash": _opt_str(body, "expected_body_hash"),
        "expected_content_hash": _opt_str(body, "expected_content_hash"),
    }


@_route("POST", "/v1/write")
def _write(body: dict[str, Any]) -> dict[str, Any]:
    path = body.get("path")
    content = body.get("content")
    text = body.get("text")
    if not isinstance(path, str) or not path.strip():
        return {"ok": False, "error": "bad_request", "message": "`path` string required"}
    if content is not None and not isinstance(content, str):
        return {"ok": False, "error": "bad_request", "message": "`content` must be a string"}
    if text is not None and not isinstance(text, str):
        return {"ok": False, "error": "bad_request", "message": "`text` must be a string"}
    if "append" in body:
        return {
            "ok": False,
            "error": "append_removed",
            "message": (
                "write_note append is removed; use POST /v1/append "
                "(or append_note MCP) with path + text"
            ),
        }
    return ops.write_note(
        path,
        content if isinstance(content, str) else None,
        text=text if isinstance(text, str) else None,
        body=body.get("body") if isinstance(body.get("body"), str) else None,
        expected_mtime=_opt_float(body, "expected_mtime"),
        vault=str(body.get("vault") or ""),
        **_region_kwargs(body),
    )


@_route("POST", "/v1/append")
def _append(body: dict[str, Any]) -> dict[str, Any]:
    path = body.get("path")
    text = body.get("text")
    content = body.get("content")
    chunk_hash = body.get("chunk_hash")
    path_s = path.strip() if isinstance(path, str) else ""
    ch_s = chunk_hash.strip() if isinstance(chunk_hash, str) else ""
    if not path_s and not ch_s:
        return {
            "ok": False,
            "error": "bad_request",
            "message": "`path` or `chunk_hash` required",
        }
    if text is not None and not isinstance(text, str):
        return {"ok": False, "error": "bad_request", "message": "`text` must be a string"}
    if content is not None and not isinstance(content, str):
        return {"ok": False, "error": "bad_request", "message": "`content` must be a string"}
    position = str(body.get("position") or "end")
    if position not in ("end", "start"):
        return {"ok": False, "error": "bad_request", "message": "`position` must be end|start"}
    heading = body.get("heading")
    if heading is not None and not isinstance(heading, str):
        return {"ok": False, "error": "bad_request", "message": "`heading` must be a string"}
    if chunk_hash is not None and not isinstance(chunk_hash, str):
        return {"ok": False, "error": "bad_request", "message": "`chunk_hash` must be a string"}
    return ops.append_note(
        path_s,
        text if isinstance(text, str) else None,
        content=content if isinstance(content, str) else None,
        body=body.get("body") if isinstance(body.get("body"), str) else None,
        heading=heading,
        chunk_hash=ch_s or None,
        position=position,  # type: ignore[arg-type]
        create=bool(body.get("create")),
        expected_mtime=_opt_float(body, "expected_mtime"),
        vault=str(body.get("vault") or ""),
        **_region_kwargs(body),
    )


@_route("POST", "/v1/patch")
def _patch(body: dict[str, Any]) -> dict[str, Any]:
    items = body.get("items")
    path = body.get("path")
    patch_ops = body.get("ops")
    if items is not None and not isinstance(items, list):
        return {
            "ok": False,
            "error": "bad_request",
            "message": "`items` must be an array when set",
        }
    if patch_ops is not None and not isinstance(patch_ops, list):
        return {"ok": False, "error": "bad_request", "message": "`ops` must be an array"}
    return ops.patch_entry(
        path=str(path or ""),
        ops=patch_ops if isinstance(patch_ops, list) else None,
        items=items if isinstance(items, list) else None,
        strict=bool(body.get("strict")),
        dry_run=bool(body.get("dry_run")),
        verbose=bool(body.get("verbose")),
        expected_mtime=_opt_float(body, "expected_mtime"),
        vault=str(body.get("vault") or ""),
        **_region_kwargs(body),
    )


@_route("POST", "/v1/patch_notes")
def _patch_notes(body: dict[str, Any]) -> dict[str, Any]:
    """Frozen alias of multi-path ``POST /v1/patch`` with ``items``."""
    items = body.get("items")
    if not isinstance(items, list):
        return {
            "ok": False,
            "error": "bad_request",
            "message": "`items` must be an array of {path, ops, expected_mtime?}",
        }
    return ops.patch_entry(
        items=items,
        strict=bool(body.get("strict")),
        dry_run=bool(body.get("dry_run")),
        verbose=bool(body.get("verbose")),
        vault=str(body.get("vault") or ""),
    )


def _place_body(body: dict[str, Any]) -> dict[str, Any]:
    src = body.get("src")
    dst = body.get("dst")
    if not isinstance(src, str) or not src.strip():
        return {"ok": False, "error": "bad_request", "message": "`src` string required"}
    if not isinstance(dst, str) or not dst.strip():
        return {"ok": False, "error": "bad_request", "message": "`dst` string required"}
    fields = body.get("fields")
    if fields is not None and not isinstance(fields, dict):
        return {"ok": False, "error": "bad_request", "message": "`fields` must be an object"}
    place_op: dict[str, Any] = {
        "op": "place",
        "src": src.strip(),
        "dst": dst.strip(),
        "overwrite": bool(body.get("overwrite")),
    }
    if isinstance(fields, dict):
        place_op["fields"] = fields
    return ops.patch_entry(
        ops=[place_op],
        expected_mtime=_opt_float(body, "expected_mtime"),
        vault=str(body.get("vault") or ""),
    )


@_route("POST", "/v1/place")
def _place(body: dict[str, Any]) -> dict[str, Any]:
    """Move if src is in the vault; otherwise copy host .md into the vault."""
    return _place_body(body)


@_route("POST", "/v1/move")
def _move(body: dict[str, Any]) -> dict[str, Any]:
    """Alias of /v1/place (prefer /v1/place)."""
    return _place_body(body)


@_route("POST", "/v1/send")
def _send(body: dict[str, Any]) -> dict[str, Any]:
    """Alias of /v1/place for host→vault copy (prefer /v1/place)."""
    return _place_body(body)


@_route("POST", "/v1/delete")
def _delete(body: dict[str, Any]) -> dict[str, Any]:
    path = body.get("path")
    if not isinstance(path, str) or not path.strip():
        return {"ok": False, "error": "bad_request", "message": "`path` string required"}
    return ops.delete_note(path, vault=str(body.get("vault") or ""))


@_route("POST", "/v1/git_sync")
def _git_sync(body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action", "status")
    if not isinstance(action, str) or not action.strip():
        return {"ok": False, "error": "bad_request", "message": "`action` string required"}
    message = body.get("message", "")
    if message is not None and not isinstance(message, str):
        return {"ok": False, "error": "bad_request", "message": "`message` must be a string"}
    return ops.git_sync_op(
        action.strip(),
        message=str(message or ""),
        vault=str(body.get("vault") or ""),
    )


@_route("POST", "/v1/list_refs")
def _list_refs(body: dict[str, Any]) -> dict[str, Any]:
    kind = body.get("kind", "heads")
    if kind is not None and not isinstance(kind, str):
        return {"ok": False, "error": "bad_request", "message": "`kind` must be a string"}
    return ops.list_refs_op(
        vault=str(body.get("vault") or ""),
        kind=str(kind or "heads"),
    )


@_route("GET", "/v1/vault")
@_route("POST", "/v1/vault")
def _vault(body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action", "list")
    if not isinstance(action, str) or not action.strip():
        return {"ok": False, "error": "bad_request", "message": "`action` string required"}
    full = body.get("full", False)
    if not isinstance(full, bool):
        return {"ok": False, "error": "bad_request", "message": "`full` must be a boolean"}
    vaults_raw = body.get("vaults")
    if vaults_raw is not None and not (
        isinstance(vaults_raw, list) and all(isinstance(v, str) for v in vaults_raw)
    ):
        return {"ok": False, "error": "bad_request", "message": "`vaults` must be a list of strings"}
    return ops.vault_op(
        action.strip(),
        vault=str(body.get("vault") or ""),
        vaults=vaults_raw,
        full=full,
        days=int(body["days"]) if body.get("days") is not None else 7,
        folder=str(body.get("folder") or ""),
        limit=int(body["limit"]) if body.get("limit") is not None else 50,
        offset=int(body["offset"]) if body.get("offset") is not None else 0,
        fix=bool(body.get("fix")),
    )


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
            from apo_engine.session_context import bind_request_session, strip_session_body

            with bind_request_session(body=body):
                body = strip_session_body(body)
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
        elif err in ("forbidden_src", "use_move_note", "too_large"):
            status = 403
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
