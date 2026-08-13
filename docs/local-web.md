# Local-web desk viewer

On-the-fly HTML preview for vaults that ship
`system/contracts/local-web-contract.schema.yaml`. **No adjacent `.html`.**

Template + schema: [contracts/local-web.md](./contracts/local-web.md).

## Start

```bash
# from apo repo (or jj workspace) — vault must have local-web-contract
just serve --vault meta
# or:
apo-engine web-serve --vault meta --port 7432
```

Open `http://127.0.0.1:7432/`.

Distinct from JSON RPC:

```bash
just rpc   # apo-engine serve → :8765
```

## Agent URLs

| URL | Role |
|-----|------|
| `/note?path=areas/foo.md&layout=memo` | Browser HTML page (+ live bar; auto-reloads on save) |
| `/api/render?path=areas/foo.md` | Default JSON `{ok,html,path,layout,title,mtime_ns,cached}` |
| `/api/render?path=…` + `Accept: text/html` | Raw HTML body (with live chrome) |
| `/api/mtime?path=…` | `{ok,path,mtime_ns}` — hot-reload poll target |
| `/search?q=…` | Thin search UI (hybrid index) |
| `/api/search?q=…` | JSON search results |
| `/health` | Liveness + cache stats |

Repeat `/note` hits are served from an in-process LRU cache (keyed by source + asset mtimes). Editing the markdown or the live contract YAML invalidates automatically.

## Export vs preview

| Need | Tool |
|------|------|
| Preview in browser | `just serve` / local-web |
| Durable HTML/PDF/Confluence/Slack | vault htmlize or Workbench `just export` |

Usage-contract `contribution.render` describes **export only**. Live preview is gated by the local-web contract.
