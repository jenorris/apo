# Contract template: Local-web (desk viewer)

**Status:** optional template · **Machine contract + thin HTTP viewer** · pairs with PARA, OKF, or llm-wiki

Use when you want a **localhost-only** browser surface over the live vault: open any in-scope note as HTML **without** writing adjacent `.html`, plus search against the Apo index. Durable export (PDF, Clinic Notification Center, Confluence fragments) stays on vault/harness **htmlize** / Workbench `just export` — gated by usage-contract `contribution.render`, not this contract.

Encode the live contract in the vault (`system/contracts/local-web-contract.schema.yaml`). This file is a **template** to copy — not live Apo config.

**Runtime:** `just serve` / `apo-engine web-serve` on `127.0.0.1` (default port **7432**). Distinct from JSON RPC `apo-engine serve` on **8765**. Env override: `APO_LOCAL_WEB_CONTRACT`.

## Stance

| Surface | Mutate | Disk write | Role |
|---------|--------|------------|------|
| **local-web** | audit files only (optional) | none for pages | Human browse + on-the-fly HTML |
| **Render API** | none | none | Same HTML for agents (`/note`, `/api/render`) |
| **htmlize / export** | none | adjacent artifacts | Durable packs |
| **MCP write tools** | full | vault notes | Authoring |

## What to encode

| Field | Meaning |
|-------|---------|
| `bind` / `port` | Loopback only (`127.0.0.1` default) |
| `mode` | `adaptive` (default) \| `para` \| `llm-wiki` |
| `browse_roots` | Empty = derive from layout dirs; explicit list wins |
| `exclude_globs` | Paths never served as notes |
| `audit` | Optional select-text → audit markdown write-back |
| `features` | search / filter / backlinks / mermaid / math toggles |
| `render` | Pandoc layout, vault-relative CSS/template, wikilink mode |

## Machine contract (encode in the vault)

Copy or adapt:

- YAML: `system/contracts/local-web-contract.schema.yaml` — starter: [local-web-contract.schema.yaml](./local-web-contract.schema.yaml)
- Optional assets under `system/assets/htmlize.css` (+ template/header). Missing assets fall back to the engine bundle.

## Adaptive browse roots

When `browse_roots` is empty and `mode` is `adaptive` (or omitted):

| Signal on disk | Contribution |
|----------------|--------------|
| PARA dirs (`projects/`, `areas/`, `resources/`, `inbox/`, …) | Those roots |
| `wiki/` + `raw/` | llm-wiki silo roots |
| Multiple | Union |
| Explicit `browse_roots` | Always wins |

`exclude_globs` always apply to `/note` and `/api/render`.

## Agent entrypoints

```text
http://127.0.0.1:7432/note?path=areas/example.md&layout=memo
http://127.0.0.1:7432/api/render?path=areas/example.md
  Accept: text/html     → HTML body
  Accept: application/json → {"ok":true,"html":"…","path":"…","layout":"…"}
```

Search UI and `GET/POST /api/search?q=…` reuse the engine hybrid index when `features.search` is true.

## Agent behaviors

1. Prefer this viewer for **preview**; do not run htmlize solely to open a note in a browser.
2. Never treat local-web as a general write surface — MCP/`append_note`/`patch_note` remain the mutators.
3. Respect `exclude_globs` (inbox/daily, audit noise, etc.).
4. Keep usage-contract `contribution.render` for export-only docs; do not conflate with live preview.

## Explicit non-goals

- Remote / Tailscale bind
- Replacing Obsidian, Quartz, or KB Gateway
- Confluence / PDF / Slack artifact generation
- Renaming the contract id away from `local-web`
