# Contract template: OKF Knowledge Bundle

**Status:** optional template · **Layout + behaviors + machine contract** · pairs with [para.md](./para.md)

Use when the vault is (or should become) an [Open Knowledge Format (OKF) v0.1](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) Knowledge Bundle: typed concepts, progressive disclosure via `index.md`, and Apo `filter_notes` on `okf_type`.

**Live contract (Meta reference):** `~/Notes/Meta/system/config/okf-contract.schema.yaml`  
**OKF conformance prose:** `system/config/okf-profile.md` (OKF “conformance profile” jargon — not an Apo preset)  
**Write-path normative:** `system/config/apo-okf-write-contract.md`

## Stance

| Layer | Role |
|-------|------|
| **OKF** | Interchange + typed query contract (`okf_type`, `description`, `timestamp`, `resource`) |
| **PARA (or other layout)** | Organization overlay |
| **Apo** | Search + mutate; loads the vault’s machine contract and stamps/validates on write |

The engine is **contract-driven** — no single vault’s taxonomy is hardcoded in `core.py`. Vaults without a machine contract get `enforcement: off`.

## Frontmatter (primary type)

**Canonical type field:** `okf_type` (OKF `type`).

Do **not** treat a Meta-style `type: note` enum as the semantic type. Prefer specific OKF types (`Project`, `Thread`, `Fact`, `EvidenceRequest`, …). Catch-all `okf_type: Note` only when nothing else fits.

```yaml
---
title: Human title
okf_type: Project
description: One-line summary
timestamp: "2026-07-17T19:51:00Z"
resource: ""
status: active
---
```

## Machine contract (encode in the vault)

Copy or adapt YAML to:

`system/config/okf-contract.schema.yaml`

(Legacy filename `okf-profile.schema.yaml` still loaded for compatibility.)

Apo then:

1. **Resolve path class** — reserved (`index.md` / `log.md`), exempt (e.g. daily session logs), concept (default), corpus (hard globs).
2. **Stamp** missing `okf_type`, `description`, `timestamp`, `resource`/`title` — never overwrite non-empty `okf_type` / `resource` on soft stamp.
3. **Validate** required fields; **soft** = warn + write; **hard** = `ok: false` with `error: okf_validation`.
4. **`append_note` v1** — no full concept stamp (History / session log append-only).

Env:

- `APO_OKF_CONTRACT` — path to YAML (alias: `APO_OKF_PROFILE`)
- `APO_OKF_ENFORCEMENT=soft|hard|off`

Offline twin in Meta: `just okf lint` / `just okf fix`.

## Agent behaviors

1. On concept `write_note` / meaningful `patch_note`: set `okf_type`, `description`, `timestamp`.
2. Prefer `filter_notes({"okf_type": "…"}, folder=…)` for typed corpora before opening dashboard/tracker notes.
3. Non-root `index.md`: **no** concept frontmatter (OKF reserved listing).
4. MCP tool names stay `*_note` — “concept” is the vocabulary; “note” is the file/tool colloquialism.

## Mixing

OKF Bundle + [PARA](./para.md) is the Meta shape. Document which `APO_NOTES_ROOT` / `folder=` applies when combining roots.

## Out of scope

- Renaming MCP tools or sqlite entity names
- Forcing OKF on vaults that omit the machine contract file
