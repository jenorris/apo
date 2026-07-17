# Profile: OKF Knowledge Bundle

**Status:** optional preset · **Layout + behaviors** · pairs with [para.md](./para.md)

Use when the vault is (or should become) an [Open Knowledge Format (OKF) v0.1](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) Knowledge Bundle: typed concepts, progressive disclosure via `index.md`, and Apo `filter_notes` on `okf_type`.

Meta vault reference implementation: `~/Notes/Meta` — `system/config/okf-profile.md`, `system/config/okf-profile.schema.yaml`, `system/config/apo-okf-write-contract.md`.

## Stance

| Layer | Role |
|-------|------|
| **OKF** | Interchange + typed query contract (`okf_type`, `description`, `timestamp`, `resource`) |
| **PARA (or other layout)** | Organization overlay |
| **Apo** | Search + mutate chokepoint; **Phase 2** stamps/validates from a machine-readable profile |

The engine stays **profile-pluggable** — no single vault’s taxonomy is hardcoded in `core.py`. Generic PARA vaults may omit this profile.

## Frontmatter (primary type)

**Canonical type field:** `okf_type` (OKF `type`).

Do **not** treat a Meta-style `type: note` enum as the semantic type. Prefer specific OKF types (`Project`, `Thread`, `Fact`, `EvidenceRequest`, …). Catch-all `okf_type: Note` only when nothing else fits.

```yaml
---
title: Human title
okf_type: Project
description: One-line summary
timestamp: "2026-07-17T19:51:00Z"
resource: ""          # optional; from source_url when present
status: active
---
```

## Conformance (write path)

Normative Meta contract (agents today; engine Phase 2):

1. **Resolve path class** — reserved (`index.md` / `log.md`), exempt (e.g. daily session logs), concept (default), corpus (hard globs).
2. **Stamp** missing `okf_type`, `description`, `timestamp`, `resource`/`title` per profile — never overwrite non-empty `okf_type` / `resource` on soft stamp.
3. **Validate** required fields; **soft** = warn + write; **hard** = `ok: false` with `error: okf_validation`.
4. **`append_note` v1** — no full concept stamp (History / session log append-only).

Machine profile shape (example keys):

- `type_field: okf_type`, `legacy_type_field: type`
- `core_required: [okf_type, description, timestamp]`
- `path_rules[]` with `match`, `okf_type`, `enforcement: exempt|soft|hard`
- `default_enforcement: soft`
- Env (Phase 2): `APO_OKF_PROFILE`, `APO_OKF_ENFORCEMENT=soft|hard|off`

Offline twin in Meta: `just okf lint` / `just okf fix`.

## Agent behaviors

1. On concept `write_note` / meaningful `patch_note`: set `okf_type`, `description`, `timestamp`.
2. Prefer `filter_notes({"okf_type": "…"}, folder=…)` for typed corpora before opening dashboard/tracker notes.
3. Non-root `index.md`: **no** concept frontmatter (OKF reserved listing).
4. MCP tool names stay `*_note` — “concept” is the vocabulary; “note” is the file/tool colloquialism.

## Mixing

OKF Bundle + [PARA](./para.md) is the Meta shape. OKF + [agentic-memory](./agentic-memory.md) Facts (`okf_type: Fact` + SPO) is valid in a separate root/collection. Document which `APO_NOTES_ROOT` / `folder=` applies.

## Out of scope here

- Renaming MCP tools or sqlite entity names
- Engine stamp implementation (track separately under Apo; Meta contract is SoT until then)
