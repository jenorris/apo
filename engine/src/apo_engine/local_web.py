"""Localhost desk viewer for local-web contract (port 7432 by default).

Read-only note HTML via shared ``render_html`` core with in-process cache and
hot-reload (poll ``/api/mtime``). Contract YAML reloads when its mtime changes.
"""

from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from apo_engine import local_web_contract as lwc
from apo_engine import ops, render_html, vaults

_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Apo local-web</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Karma:wght@500;600&display=swap" rel="stylesheet"/>
<style>
  :root {{
    color-scheme: light dark;
    --ink: #2b2b2b;
    --muted: #6b6560;
    --bg: #f7f4ef;
    --card: #fffcf7;
    --accent: #c85700;
    --rule: #e8e4e0;
    --shadow: 0 1px 2px rgba(43,43,43,.04), 0 8px 24px rgba(43,43,43,.06);
    --font: "Inter", system-ui, sans-serif;
    --display: "Karma", Georgia, serif;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --ink: #ebebec; --muted: #a8a29a; --bg: #121214; --card: #1c1c1f;
      --accent: #ffb020; --rule: #3a3836;
      --shadow: 0 1px 2px rgba(0,0,0,.3), 0 8px 24px rgba(0,0,0,.35);
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh;
    font: 15px/1.5 var(--font); color: var(--ink);
    background:
      radial-gradient(1200px 600px at 10% -10%, rgba(232,184,23,.12), transparent 55%),
      radial-gradient(900px 500px at 100% 0%, rgba(200,87,0,.08), transparent 50%),
      var(--bg);
  }}
  .top {{
    display: flex; gap: 1rem; align-items: center;
    padding: .7rem 1.1rem; border-bottom: 1px solid var(--rule);
    background: color-mix(in srgb, var(--card) 88%, transparent);
    backdrop-filter: blur(10px); position: sticky; top: 0; z-index: 20;
  }}
  .brand {{
    font-family: var(--display); font-weight: 600; font-size: 1.15rem;
    color: var(--accent); text-decoration: none; letter-spacing: -0.01em;
  }}
  .brand span {{ color: var(--muted); font-weight: 500; font-size: .7rem;
    font-family: var(--font); letter-spacing: .06em; text-transform: uppercase;
    margin-left: .45rem; vertical-align: middle; }}
  form {{ display: flex; gap: .5rem; flex: 1; max-width: 36rem; }}
  input[type=search] {{
    flex: 1; padding: .45rem .7rem; border: 1px solid var(--rule);
    border-radius: 8px; background: var(--card); color: inherit; font: inherit;
  }}
  input[type=search]:focus {{ outline: 2px solid color-mix(in srgb, var(--accent) 45%, transparent); outline-offset: 1px; }}
  button {{
    padding: .45rem .9rem; border: 0; border-radius: 8px;
    background: var(--accent); color: #fff; font-weight: 600; cursor: pointer; font: inherit;
  }}
  button:hover {{ filter: brightness(1.05); }}
  main {{ max-width: 44rem; margin: 1.75rem auto; padding: 0 1.1rem 3.5rem; }}
  .panel {{
    background: var(--card); border: 1px solid var(--rule); border-radius: 12px;
    padding: 1.25rem 1.35rem; box-shadow: var(--shadow);
  }}
  .hit {{
    display: block; padding: .85rem 0; border-bottom: 1px solid var(--rule);
    color: inherit; text-decoration: none;
  }}
  .hit:last-child {{ border-bottom: 0; }}
  .hit:hover .hit-title {{ color: var(--accent); }}
  .hit-title {{ font-weight: 600; }}
  .path {{ font-size: .8rem; color: var(--muted); font-family: ui-monospace, Menlo, monospace; margin-top: .15rem; }}
  .snip {{ margin: .35rem 0 0; color: var(--muted); font-size: .92rem; }}
  .meta {{ font-size: .85rem; color: var(--muted); margin: 0 0 1rem; }}
  .meta code {{ font-size: .8rem; background: color-mix(in srgb, var(--accent) 10%, transparent);
    padding: .1rem .35rem; border-radius: 4px; }}
  h1.page-title {{
    font-family: var(--display); font-size: 1.75rem; font-weight: 600;
    margin: 0 0 .35rem; letter-spacing: -0.01em;
  }}
  .err {{
    color: #9b1c1c; padding: 1rem 1.1rem; background: var(--card);
    border: 1px solid #f0c4c4; border-radius: 10px;
  }}
  @media (prefers-color-scheme: dark) {{
    .err {{ color: #f97066; border-color: #5c2a2a; }}
  }}
  kbd {{
    font: .75rem ui-monospace, Menlo, monospace; border: 1px solid var(--rule);
    border-radius: 4px; padding: .05rem .3rem; background: var(--card);
  }}
</style>
</head>
<body>
<header class="top">
  <a class="brand" href="/">Apo<span>local-web</span></a>
  <form method="get" action="/search"><input type="search" name="q" placeholder="Search notes…" value="{q}" autofocus/><button type="submit">Search</button></form>
</header>
<main>
{body}
</main>
</body>
</html>
"""

# Injected into rendered note pages (before </body>).
_LIVE_SNIPPET = """
<style id="apo-live-chrome">
  :root {{ --apo-bar-h: 2.4rem; --toc-top: calc(var(--apo-bar-h) + 1rem); }}
  body {{ padding-top: var(--apo-bar-h); }}
  .apo-live-bar {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 1000;
    display: flex; align-items: center; gap: .75rem;
    height: var(--apo-bar-h); padding: 0 .9rem;
    font: 500 .75rem/1 Inter, system-ui, sans-serif;
    letter-spacing: .02em;
    color: var(--ink-muted, #525252);
    background: color-mix(in srgb, var(--paper, #fff) 86%, transparent);
    border-bottom: 1px solid var(--rule, #e8e4e0);
    backdrop-filter: blur(10px);
  }}
  .apo-live-bar a {{ color: var(--accent, #c85700); text-decoration: none; font-weight: 600; }}
  .apo-live-bar .path {{
    flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    font-family: ui-monospace, Menlo, monospace; font-weight: 400; opacity: .85;
  }}
  .apo-live-bar .status {{
    display: inline-flex; align-items: center; gap: .35rem;
    text-transform: uppercase; letter-spacing: .06em; font-size: .65rem;
  }}
  .apo-live-bar .dot {{
    width: .45rem; height: .45rem; border-radius: 50%;
    background: #3f6212; box-shadow: 0 0 0 3px rgba(63,98,18,.15);
  }}
  .apo-live-bar.reloading .dot {{
    background: var(--accent, #c85700);
    box-shadow: 0 0 0 3px rgba(200,87,0,.18);
    animation: apo-pulse 0.9s ease-in-out infinite;
  }}
  @keyframes apo-pulse {{ 50% {{ opacity: .45; }} }}
  @media (prefers-color-scheme: dark) {{
    .apo-live-bar .dot {{ background: #a3e635; box-shadow: 0 0 0 3px rgba(163,230,53,.18); }}
  }}
  @media print {{ .apo-live-bar {{ display: none; }} body {{ padding-top: 0; }} }}
</style>
<div class="apo-live-bar" id="apo-live-bar" data-path="{path}" data-mtime="{mtime}">
  <a href="/">Apo</a>
  <span class="path" title="{path}">{path}</span>
  <span class="status"><span class="dot" aria-hidden="true"></span><span id="apo-live-label">live</span></span>
</div>
<script>
(function () {{
  var bar = document.getElementById("apo-live-bar");
  if (!bar) return;
  var path = bar.getAttribute("data-path");
  var mtime = bar.getAttribute("data-mtime");
  var label = document.getElementById("apo-live-label");
  var timer = setInterval(function () {{
    fetch("/api/mtime?path=" + encodeURIComponent(path), {{ headers: {{ Accept: "application/json" }} }})
      .then(function (r) {{ return r.json(); }})
      .then(function (data) {{
        if (!data || !data.ok) return;
        var next = data.stamp || String(data.mtime_ns);
        if (next !== mtime) {{
          bar.classList.add("reloading");
          if (label) label.textContent = "reload";
          clearInterval(timer);
          location.reload();
        }}
      }})
      .catch(function () {{}});
  }}, 1500);
}})();
</script>
"""


def _html_page(body: str, q: str = "") -> bytes:
    return _SHELL.format(body=body, q=_escape(q)).encode("utf-8")


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _clean_heading(s: str) -> str:
    """Strip inline markdown emphasis from indexed heading breadcrumbs."""
    return re.sub(r"(\*\*|__|`)", "", s).strip()


def _dedupe_hits(hits: Any) -> list[dict[str, Any]]:
    """Hybrid search returns a chunk once per arm; keep the first occurrence."""
    seen: set[Any] = set()
    out: list[dict[str, Any]] = []
    for h in hits if isinstance(hits, list) else []:
        if not isinstance(h, dict):
            continue
        key = h.get("chunk_hash") or (h.get("source"), h.get("heading"))
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def _json_bytes(payload: dict[str, Any], status: int = 200) -> tuple[int, bytes, str]:
    return status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8"


def inject_live_chrome(html: str, *, path: str, mtime_ns: int) -> str:
    """Append live bar + hot-reload poller before ``</body>``."""
    snippet = _LIVE_SNIPPET.format(path=_escape(path), mtime=str(mtime_ns))
    lower = html.lower()
    idx = lower.rfind("</body>")
    if idx == -1:
        return html + snippet
    return html[:idx] + snippet + html[idx:]


class LocalWebHandler(BaseHTTPRequestHandler):
    vault_name: str = ""
    vault_root: Path = Path()
    contract: dict[str, Any] = {}
    features: dict[str, bool] = {}
    contract_path: Path | None = None
    _contract_mtime_ns: int = 0
    _state_lock = threading.Lock()

    def log_message(self, fmt: str, *args: Any) -> None:
        sys_stderr = __import__("sys").stderr
        print(f"[local-web] {self.address_string()} {fmt % args}", file=sys_stderr)

    def _maybe_reload_contract(self) -> None:
        """Hot-reload live YAML when its mtime changes."""
        path = self.contract_path
        if path is None or not path.is_file():
            return
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return
        with self._state_lock:
            if mtime_ns == self._contract_mtime_ns:
                return
            data = lwc.load_local_web_contract(self.vault_root)
            if data is None:
                return
            type(self).contract = data
            type(self).features = lwc.resolve_features(data)
            type(self)._contract_mtime_ns = mtime_ns
            render_html.clear_render_cache()

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _wants_json(self) -> bool:
        accept = self.headers.get("Accept", "")
        return "application/json" in accept and "text/html" not in accept.split(",")[0]

    def do_GET(self) -> None:  # noqa: N802
        self._maybe_reload_contract()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        if path == "/health":
            status, body, ctype = _json_bytes(
                {
                    "ok": True,
                    "service": "apo-local-web",
                    "vault": self.vault_name,
                    "root": str(self.vault_root),
                    "port": self.server.server_address[1],  # type: ignore[attr-defined]
                    "cache": render_html.cache_stats(),
                }
            )
            self._send(status, body, ctype)
            return

        if path == "/api/mtime":
            self._handle_mtime(qs)
            return

        if path == "/note":
            self._handle_note(qs)
            return

        if path == "/api/render":
            self._handle_render(qs)
            return

        if path in ("/search", "/api/search"):
            self._handle_search(qs, api=path.startswith("/api"))
            return

        if path == "/":
            self._handle_home()
            return

        self._send(404, _html_page('<p class="err">Not found</p>'), "text/html; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        self._maybe_reload_contract()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send(400, b'{"ok":false,"error":"bad_json"}', "application/json; charset=utf-8")
            return
        if path == "/api/search":
            q = str(body.get("query") or body.get("q") or "")
            self._handle_search({"q": [q]}, api=True)
            return
        if path == "/api/render":
            qs = {
                "path": [str(body.get("path") or "")],
                "layout": [str(body.get("layout") or "")] if body.get("layout") else [],
            }
            self._handle_render(qs)
            return
        self._send(404, b'{"ok":false,"error":"not_found"}', "application/json; charset=utf-8")

    def _handle_mtime(self, qs: dict[str, list[str]]) -> None:
        rel = unquote((qs.get("path") or [""])[0]).strip()
        if not rel:
            status, body, ctype = _json_bytes(
                {"ok": False, "error": "bad_request", "message": "missing path"}, 400
            )
            self._send(status, body, ctype)
            return
        allowed, reason = lwc.is_path_allowed(self.vault_root, rel, data=self.contract)
        if not allowed:
            status, body, ctype = _json_bytes(
                {"ok": False, "error": "forbidden", "message": reason}, 403
            )
            self._send(status, body, ctype)
            return
        try:
            mtime_ns = render_html.note_mtime_ns(self.vault_root, rel)
        except render_html.RenderError as e:
            http = 404 if e.code == "not_found" else 400
            status, body, ctype = _json_bytes(
                {"ok": False, "error": e.code, "message": e.message}, http
            )
            self._send(status, body, ctype)
            return
        status, body, ctype = _json_bytes(
            {
                "ok": True,
                "path": rel.replace("\\", "/").lstrip("/"),
                "mtime_ns": mtime_ns,
                # ns mtimes exceed JS Number precision; poller must compare the string.
                "stamp": str(mtime_ns),
            }
        )
        self._send(status, body, ctype)

    def _handle_home(self) -> None:
        roots = lwc.resolve_browse_roots(self.vault_root, self.contract)
        root_s = ", ".join(f"<code>{_escape(r)}</code>" for r in roots) or "<em>entire vault</em>"
        cache = render_html.cache_stats()
        body = (
            '<div class="panel">'
            f'<h1 class="page-title">{_escape(self.vault_name)}</h1>'
            f'<p class="meta">Browse roots: {root_s} · cache {cache["entries"]}/{cache["max"]}</p>'
            "<p>Search above, or open a note: "
            "<code>/note?path=areas/example.md</code>.</p>"
            "<p>Notes hot-reload when the markdown file changes on disk. "
            "Edit the local-web contract YAML to refresh bind/exclude/render without restart.</p>"
            "<p>Agents: <code>/api/render?path=…</code> "
            "(<code>Accept: application/json</code> or <code>text/html</code>) · "
            "<code>/api/mtime?path=…</code>.</p>"
            "</div>"
        )
        self._send(200, _html_page(body), "text/html; charset=utf-8")

    def _handle_note(self, qs: dict[str, list[str]]) -> None:
        rel = unquote((qs.get("path") or [""])[0]).strip()
        layout = ((qs.get("layout") or [""])[0]).strip() or None
        if not rel:
            self._send(400, _html_page('<p class="err">missing path=</p>'), "text/html; charset=utf-8")
            return
        try:
            result = render_html.render_note_html(
                self.vault_root,
                rel,
                layout=layout,
                contract=self.contract,
            )
        except render_html.RenderError as e:
            status = 403 if e.code == "forbidden" else 404 if e.code == "not_found" else 500
            self._send(
                status,
                _html_page(f'<p class="err">{_escape(e.code)}: {_escape(e.message)}</p>'),
                "text/html; charset=utf-8",
            )
            return
        html = inject_live_chrome(
            result["html"], path=result["path"], mtime_ns=int(result["mtime_ns"])
        )
        self._send(
            200,
            html.encode("utf-8"),
            "text/html; charset=utf-8",
            extra_headers={
                "X-Apo-Cached": "1" if result.get("cached") else "0",
                "X-Apo-Mtime-Ns": str(result["mtime_ns"]),
            },
        )

    def _handle_render(self, qs: dict[str, list[str]]) -> None:
        rel = unquote((qs.get("path") or [""])[0]).strip()
        layout = ((qs.get("layout") or [""])[0]).strip() or None
        if not rel:
            status, body, ctype = _json_bytes(
                {"ok": False, "error": "bad_request", "message": "missing path"}, 400
            )
            self._send(status, body, ctype)
            return
        try:
            result = render_html.render_note_html(
                self.vault_root,
                rel,
                layout=layout,
                contract=self.contract,
            )
        except render_html.RenderError as e:
            http = 403 if e.code == "forbidden" else 404 if e.code == "not_found" else 500
            if self._wants_json() or "application/json" in self.headers.get("Accept", ""):
                status, body, ctype = _json_bytes(
                    {"ok": False, "error": e.code, "message": e.message}, http
                )
                self._send(status, body, ctype)
            else:
                self._send(
                    http,
                    _html_page(f'<p class="err">{_escape(e.code)}: {_escape(e.message)}</p>'),
                    "text/html; charset=utf-8",
                )
            return

        accept = self.headers.get("Accept", "")
        if "text/html" in accept and (
            "application/json" not in accept.split(",")[0]
            or accept.strip().startswith("text/html")
        ):
            html = inject_live_chrome(
                result["html"], path=result["path"], mtime_ns=int(result["mtime_ns"])
            )
            self._send(
                200,
                html.encode("utf-8"),
                "text/html; charset=utf-8",
                extra_headers={"X-Apo-Cached": "1" if result.get("cached") else "0"},
            )
            return
        status, body, ctype = _json_bytes(
            {
                "ok": True,
                "path": result["path"],
                "layout": result["layout"],
                "title": result["title"],
                "html": result["html"],
                "mtime_ns": result["mtime_ns"],
                "cached": bool(result.get("cached")),
            }
        )
        self._send(status, body, ctype)

    def _handle_search(self, qs: dict[str, list[str]], *, api: bool) -> None:
        if not self.features.get("search", True):
            if api:
                status, body, ctype = _json_bytes(
                    {"ok": False, "error": "disabled", "message": "search disabled"}, 403
                )
                self._send(status, body, ctype)
            else:
                self._send(
                    403,
                    _html_page('<p class="err">search disabled in contract</p>'),
                    "text/html; charset=utf-8",
                )
            return
        q = (qs.get("q") or qs.get("query") or [""])[0].strip()
        if not q:
            if api:
                status, body, ctype = _json_bytes(
                    {"ok": False, "error": "bad_request", "message": "missing q"}, 400
                )
                self._send(status, body, ctype)
            else:
                self._send(
                    200,
                    _html_page('<div class="panel"><p>Enter a query.</p></div>', q=""),
                    "text/html; charset=utf-8",
                )
            return

        roots = lwc.resolve_browse_roots(self.vault_root, self.contract)
        folder = roots[0] if len(roots) == 1 else ""
        result = ops.search(
            q,
            limit=20,
            folder=folder,
            vault=self.vault_name,
            exclude=lwc.resolve_exclude_globs(self.contract) or None,
        )
        if api or self._wants_json():
            status, body, ctype = _json_bytes(result, 200 if result.get("ok") else 500)
            self._send(status, body, ctype)
            return

        if not result.get("ok"):
            msg = _escape(str(result.get("message") or result.get("error") or "search failed"))
            self._send(500, _html_page(f'<p class="err">{msg}</p>', q=q), "text/html; charset=utf-8")
            return

        hits = _dedupe_hits(result.get("results") or result.get("hits") or [])
        parts = [
            '<div class="panel">',
            f'<p class="meta">{len(hits)} hits for <strong>{_escape(q)}</strong></p>',
        ]
        for h in hits:
            p = str(h.get("source") or h.get("path") or "")
            snip = str(h.get("content") or h.get("snippet") or h.get("text") or "")[:240]
            title = _clean_heading(str(h.get("heading") or "")) or Path(p).stem
            parts.append(
                f'<a class="hit" href="/note?path={quote(p)}">'
                f'<div class="hit-title">{_escape(title)}</div>'
                f'<div class="path">{_escape(p)}</div>'
                f'<p class="snip">{_escape(snip)}</p></a>'
            )
        if len(hits) == 0:
            parts.append("<p>No results.</p>")
        parts.append("</div>")
        self._send(200, _html_page("\n".join(parts), q=q), "text/html; charset=utf-8")


def run_local_web(
    *,
    vault: str = "",
    host: str | None = None,
    port: int | None = None,
) -> None:
    """Bind and serve until interrupted."""
    default, bindings = vaults.load_bindings()
    key = (vault or "").strip() or default
    if key not in bindings:
        raise SystemExit(f"unknown vault {key!r}; available: {sorted(bindings)}")
    binding = bindings[key]
    root = binding.resolved().root
    contract_path = lwc.resolve_local_web_contract_path(root)
    contract = lwc.load_local_web_contract(root)
    if contract is None:
        raise SystemExit(
            f"vault {key!r} has no local-web-contract "
            f"(expected {lwc.LOCAL_WEB_CONTRACT_REL})"
        )

    cfg_host, cfg_port = lwc.resolve_bind_port(contract)
    bind = host or os.environ.get("APO_LOCAL_WEB_HOST", "").strip() or cfg_host
    listen_port = (
        port
        if port is not None and port > 0
        else int(os.environ.get("APO_LOCAL_WEB_PORT", "0") or 0) or cfg_port
    )
    if bind in ("0.0.0.0", "::", "[::]"):
        bind = "127.0.0.1"

    features = lwc.resolve_features(contract)
    contract_mtime = 0
    if contract_path is not None and contract_path.is_file():
        contract_mtime = contract_path.stat().st_mtime_ns

    class _Handler(LocalWebHandler):
        pass

    _Handler.vault_name = key
    _Handler.vault_root = root
    _Handler.contract = contract
    _Handler.features = features
    _Handler.contract_path = contract_path
    _Handler._contract_mtime_ns = contract_mtime

    server = ThreadingHTTPServer((bind, listen_port), _Handler)
    print(
        f"[local-web] http://{bind}:{server.server_address[1]}/  vault={key}  root={root}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[local-web] stopped", flush=True)
    finally:
        server.server_close()
