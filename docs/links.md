# Typed links

The index stores every outbound link of a note as a typed, resolved row in
`backlinks`. `backlinks`, `graph_neighbors` and the search link boosts read it.
One extractor (`engine/src/apo_engine/links.py`) produces the rows.

## What counts as a link

| Kind | Source | Resolves by |
|------|--------|-------------|
| `wikilink` | `[[name]]`, `[[path/name]]`, `[[name\|alias]]`, `[[name#heading]]` in the body | exact vault path, else a unique stem or `title` |
| `md_link` | `[text](rel/path.md#h)` in the body, relative to the note's folder (`/x.md` = vault root) | exact vault path |
| `related`, `supersedes`, `source`, `resource` | frontmatter link fields (defaults below) | path values: relative to the note's folder; `[[x]]` or bare names: like a wikilink |

Not links:

- anything inside fenced or inline code
- images and media embeds (`![..](..)`, `![[x.png]]`)
- `#heading`-only anchors
- body URLs

URLs count only when they come from a link field (`sources[].resource`, for
example). They are stored as `external`.

Default link fields, used when a vault declares none:
`related`, `supersedes`, `sources.resource` (each list element's `resource`),
and `resource`. YAML and `.mmd` notes contribute link fields only.

## Declaring link fields in the OKF contract

A vault's OKF contract (`system/contracts/okf-contract.schema.yaml`) decides
which frontmatter fields are links. Each entry has a `key`, which is a
frontmatter path (dots, `[N]`, `[id=x]`, with lists fanned out), and a `kind`,
which is the edge kind (a slug). A bare string is shorthand: `parents` means
`{key: parents, kind: parents}`.

```yaml
link_fields:                    # replaces the defaults; [] = no frontmatter links
  - {key: related,          kind: related}
  - {key: sources.resource, kind: source}
  - {key: depends_on,       kind: depends-on}
path_rules:
  - match: "adr/**"
    link_fields:                # extends the top-level list for matching notes
      - {key: supersedes, kind: supersedes}
```

- **Defaults:** with no contract, or with `link_fields` omitted, the defaults
  above apply.
- **Validation:** `vault(contracts)` and `vault(lint)` report malformed
  entries, which are dropped on load.
- **Re-extraction:** a change to any link-field declaration re-extracts links
  without re-embedding. The watcher does it as soon as the contract file
  changes, and the periodic reconcile scan (`WATCH_RECONCILE_INTERVAL`) checks
  again. Meta `links_schema_version` stores
  the extractor version plus a fingerprint of the declarations.

## Status

| `status` | Meaning |
|----------|---------|
| `resolved` | `target_path` is an indexed note |
| `ambiguous` | a bare name matches more than one note (no target; neither note gets the backlink) |
| `dangling` | no indexed note matches |
| `external` | URL, or a path outside the vault root |
| `foreign` | `[[vault:path]]`, so `target_vault` is set (it is not resolved across vaults) |
| `unresolved` | legacy row from before the typed-link schema, waiting for backfill |

Resolution is set-based and runs at the end of every index batch, scan and purge.
A link written before its target exists resolves when the target is indexed.
Deleting or moving a target turns its inbound path links `dangling`. A name or
title link follows a moved note.

## Upgrade

The schema change is additive: new columns on `backlinks`, plus generated
`files.link_*` columns that only the writer adds. On the first scan after
upgrade, the watcher re-extracts links for every indexed note. It does not
re-embed. The meta key `links_schema_version` gates this. Readers on an
index that has not migrated yet fall back to the old wiki-link key matching.

Manual backfill, with the watcher stopped:

```bash
apo-engine index --links-only
```

## Graph export

`vault(action=export)` (MCP) and `apo-local graph-export` (CLI) return the
link graph as networkx node-link JSON, which is the hand-off format for
renderers:

```json
{"directed": true, "multigraph": true,
 "graph": {"vault": "work", "generated_at": "…", "focus": "x.md", "depth": 1},
 "nodes": [{"id": "x.md", "kind": "note", "title": "X", "okf_type": "Reference"}],
 "links": [{"source": "x.md", "target": "y.md", "key": "related", "kind": "related",
            "status": "resolved", "count": 1}]}
```

Scope options:

- **Whole vault:** the default.
- **`folder=`:** notes under the folder, keeping only links with both ends inside.
- **`path=` + `depth=` (1–3):** an ego graph. It walks resolved links in both
  directions, and the focus note is always the first node, which suits a
  per-page local graph.

Filters:

- `kinds=` keeps only the listed edge kinds.
- `include_unresolved=true` adds `dangling`, `ambiguous`, `external` and
  `foreign` target nodes.
- `max_nodes` caps the node count and sets `truncated`. The MCP action
  defaults to 500; the CLI has no cap.

```bash
apo-local graph-export --path areas/x.md --depth 2 -o /tmp/x-graph.json
```

## Querying

- `graph_neighbors(path, kinds=[...])` returns edges with `kind` and `status`.
- `apo-local graph-neighbors PATH --kind related --kind md_link` is the CLI form.
- `backlinks(path)` returns inbound links of every kind. Use `graph_neighbors`
  with `direction=in` to filter by kind.
