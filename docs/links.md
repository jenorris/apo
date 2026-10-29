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

## Querying

- `graph_neighbors(path, kinds=[...])` returns edges with `kind` and `status`.
- `apo-local graph-neighbors PATH --kind related --kind md_link` is the CLI form.
- `backlinks(path)` returns inbound links of every kind. Use `graph_neighbors`
  with `direction=in` to filter by kind.
