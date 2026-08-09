# Contract template: OKF Knowledge Bundle

**Status:** optional template · **Layout + behaviors + machine contract** · pairs with [para.md](./para.md)

Use when the vault is (or should become) an [Open Knowledge Format (OKF) v0.1](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) Knowledge Bundle: typed concepts, progressive disclosure via `index.md`, and Apo `filter_notes` on `okf_type`.

**Live contract (Meta reference):** `~/Notes/Meta/system/contracts/okf-contract.schema.yaml`
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

**Canonical type field:** `okf_type` (Apo-native) — **plus** `type`, the OKF
interchange field the spec always requires. Apo stamps both; see
[Conformance](#conformance-apo--okf-v01--v02) for the policy that decides
when `type` is written.

Do **not** treat a Meta-style `type: note` enum as the semantic type. Prefer specific OKF types (`Project`, `Thread`, `Fact`, `EvidenceRequest`, …). Catch-all `okf_type: Note` only when nothing else fits.

```yaml
---
title: Human title
type: Project          # OKF interchange (SPEC §11) — required by the spec
okf_type: Project      # Apo-native; what filter_notes queries
description: One-line summary
timestamp: "2026-07-17T19:51:00Z"
resource: ""
status: active
---
```

**Standalone YAML catalog notes** (same fields, no Markdown body) are also first-class:

```yaml
# projects/…/records/example.yaml
title: Human title
okf_type: Fact
description: One-line summary
timestamp: "2026-08-03T05:00:00Z"
status: open
```

Prefer YAML for structure-only atoms (queues, inventories, thin trackers). Prefer Markdown when you need headings, `append_note`, History, or wiki-links. `append_note` / heading patch ops reject YAML with `unsupported_format`.

## Machine contract (encode in the vault)

Copy or adapt YAML to:

`system/contracts/okf-contract.schema.yaml`

(Legacy filename `okf-profile.schema.yaml` still loaded for compatibility.)

Apo then:

1. **Resolve path class** — reserved (`index.md` / `log.md`), exempt (e.g. daily session logs), concept (default), corpus (hard globs).
2. **Stamp** missing `okf_type`, `description`, `timestamp`, `resource`/`title` — never overwrite non-empty `okf_type` / `resource` on soft stamp.
3. **Validate** required fields; **soft** = warn + write; **hard** = `ok: false` with `error: okf_validation`.
4. **`append_note` v1** — no full concept stamp (History / session log append-only).

Env:

- `APO_OKF_CONTRACT` — path to YAML (alias: `APO_OKF_PROFILE`)
- `APO_OKF_ENFORCEMENT=soft|hard|off`
- `APO_OKF_SPEC_TYPE=fill|mirror|off` — overrides `spec_type_policy`

CLI (one implementation; `vault-tools/tools/okf/` are shims over it):

```sh
just okf validate --vault meta --profile okf   # SPEC §11 exactly
just okf validate --vault meta                 # Apo producer profile (default)
just okf fix      --vault meta                 # stamp gaps via the normal write path
just okf init     --vault-root ~/Notes/New     # scaffold contract + bundle root
just okf export   --vault meta /tmp/bundle --okf-version 0.2
just okf ingest   /tmp/foreign-bundle --name foreign   # mount read-only
```

## Agent behaviors

1. On concept `write_note` / meaningful `patch_note`: set `okf_type`, `description`, `timestamp`.
2. Prefer `filter_notes({"okf_type": "…"}, folder=…)` for typed corpora before opening dashboard/tracker notes.
3. Non-root `index.md`: **no** concept frontmatter (OKF reserved listing).
4. MCP tool names stay `*_note` — “concept” is the vocabulary; “note” is the file/tool colloquialism.

## Conformance: Apo ↔ OKF v0.1 ↔ v0.2

Field-by-field state of Apo against the [OKF SPEC](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md).
**match** = emitted and read per spec · **read-only** = read but never
emitted · **partial** = present under a different name or shape ·
**conflict** = spec disagrees with Apo.

### Core

| Apo field | OKF v0.1 | OKF v0.2 | State | Note |
|-----------|----------|----------|-------|------|
| `okf_type` | `type` | `type` | **partial** | Apo-native field; the spec name is emitted alongside it (below) |
| `type` | `type` (required) | `type` (only always-required key) | **match** | Emitted by `spec_type_policy` since Phase 0 |
| `description` | recommended | recommended | **match** | Apo requires it on producer writes — stricter than spec, allowed |
| `title` | recommended | recommended | **match** | |
| `resource` | recommended | recommended | **match** | |
| `tags` | recommended | recommended | **match** | Passed through untouched |
| `timestamp` | core | **superseded** by `generated.at` | **partial** | v0.2 consumers MAY fall back to `timestamp`; Apo still emits it |

### v0.2 families

| Field | Family | State | Note |
|-------|--------|-------|------|
| `generated: {by, at}` | trust | **match** | Read always; emitted when `generated_policy: forward` |
| `verified: [{by, at}]` | trust | **read-only** | Read (bare mapping → one-element list per §11); Apo never asserts verification itself |
| `sources: [{resource, …}]` | provenance | **read-only** | Read, with `# Citations` fallback; not yet emitted |
| `usage_window: {from, to}` | provenance | **read-only** | Read; frames every `usage_count` beneath it |
| `usage_count` | provenance | **conflict** | Lives *inside* a `sources[]` entry and counts uses of that source. Apo's `metrics.duckdb` counts MCP tool calls — a different quantity. Not a wiring task. |
| `status` | lifecycle | **partial** | Read, default `stable`; unknown values surfaced not rejected. Apo vaults already use `status` with their own values |
| `stale_after` | lifecycle | **read-only** | Read; `ConceptMeta.is_stale()` |
| `runtime` / `parameters` / `computation` / `executor` / `attester` | computation | **out of scope** | Attested Computation is not an Apo concept type |

### Structural (§11 conformance clauses)

| Clause | State | Note |
|--------|-------|------|
| Every non-reserved `.md` has parseable frontmatter | **partial** | Paths marked `enforcement: exempt` (e.g. `inbox/daily/*.md`) may carry only `timestamp`, so they satisfy clause 1 but not clause 2 |
| Every frontmatter block has non-empty `type` | **match** on stamped concepts, **gap** on `exempt` paths | `--profile=okf` reports the gap rather than silently stamping daily logs |
| Reserved filenames carry no concept frontmatter | **match** | `enforcement: reserved` on `**/index.md`, `**/log.md` |
| Consumers must not reject on unknown keys / types | **match** | `--profile=okf` checks only the three clauses above |

> [!note] Producer strictness is deliberate
> Apo's producer profile requires `okf_type` + `description` + `timestamp`.
> SPEC §11 forbids a *consumer* rejecting a bundle for missing optional
> fields, but says nothing against a stricter producer. The two profiles are
> therefore split rather than merged: `--profile=apo` is the house style,
> `--profile=okf` is what an outside consumer is allowed to demand.

### Type emission policy

`spec_type_policy` in the vault's `okf-contract.schema.yaml`
(env override `APO_OKF_SPEC_TYPE`):

| Policy | Behavior |
|--------|----------|
| `fill` (default) | Write `type` only when absent. A vault using `type` as a legacy taxonomy (`legacy_type_map`) keeps its own values and is still conformant, since the spec requires only that `type` be non-empty and forbids rejecting unknown type values. |
| `mirror` | Force `type` to the resolved OKF type, overwriting a legacy value. Choose this when the vault has no separate taxonomy to preserve. |
| `off` | Never emit `type`. The vault is then not a conformant bundle; exports still get `type` added at export time. |

### Producer provenance (v0.2)

`generated: {by, at}` supersedes the v0.1 `timestamp` (SPEC §13.1). Emission is
**opt-in and forward-only** — set in the vault contract:

```yaml
generated_policy: forward        # off (default) | forward
generated_by: "apo/engine"       # SPEC §7 actor: <producer>/<version>, human:<id>, process:<id>
```

(env override `APO_OKF_GENERATED=off|forward`)

| Behavior | Rule |
|----------|------|
| New concept | `generated` stamped alongside `timestamp` |
| Existing concept with `generated` | Left alone unless this write also refreshed `timestamp` |
| Existing concept without `generated` | **Not backfilled** |

> [!warning] Why no backfill
> The engine does not know who generated content it did not write. Stamping
> `generated: {by: apo/engine}` on an old note would assert authorship that
> never happened, and `generated.by` is exactly what the trust family keys on.
> An unstamped note simply falls back to `timestamp`, which the dual-version
> read already handles.

`timestamp` keeps being written too, so v0.1 consumers are unaffected.

## Mixing

OKF Bundle + [PARA](./para.md) is the Meta shape. Document which `APO_NOTES_ROOT` / `folder=` applies when combining roots.

## Out of scope

- Renaming MCP tools or sqlite entity names
- Forcing OKF on vaults that omit the machine contract file
