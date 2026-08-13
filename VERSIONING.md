# Versioning & release

Semver for `apo-engine` (`MAJOR.MINOR.PATCH`). Tags are `vX.Y.Z`.
Changelog is the SoT for what shipped; GitHub Releases are the public cut notes.

## When to bump

| Bump | Use when |
|------|----------|
| **MAJOR** | Breaking MCP/RPC tool or schema changes agents cannot ignore |
| **MINOR** | New capabilities or intentional product/positioning shifts (compat preserved) |
| **PATCH** | Fixes, docs-only follow-ups that you want tagged, small ergonomics |

Docs-only commits may land on `main` **without** a version bump (this file’s first ship is an example). Tag when you want a named install/upgrade checkpoint.

## Files that must stay in sync

1. [`engine/pyproject.toml`](engine/pyproject.toml) — `[project].version`
2. [`engine/src/apo_engine/__init__.py`](engine/src/apo_engine/__init__.py) — `__version__`
3. [`CHANGELOG.md`](CHANGELOG.md) — move `## [Unreleased]` bullets under `## [X.Y.Z] — YYYY-MM-DD`

Do not leave orphan Unreleased prose without a version heading (restore a prior section if a patch shipped untagged).

## Cut checklist (jj workspace)

Work from a dedicated workspace (e.g. `~/Code/apo-worktrees/<slug>`), not the primary clone as an edit cwd.

```bash
cd ~/Code/apo-worktrees/<slug>
jj git fetch
jj new -r main          # or continue an existing feature bookmark on main tip
```

1. **Changelog** — Promote Unreleased → `## [X.Y.Z] — YYYY-MM-DD` with Added/Changed/Fixed. Keep an empty `## [Unreleased]` at the top. Note upgrade cues when MCP schemas change (`Quit Cursor/Claude fully (Cmd+Q)`).
2. **Bump** — Set the same semver in `engine/pyproject.toml` and `apo_engine.__version__`.
3. **Describe** — One commit message like `Release Apo X.Y.Z: <one-line why>`.
4. **Merge to main**

```bash
jj bookmark set main -r @
jj git push --bookmark main
```

5. **Tag + push**

```bash
jj tag set vX.Y.Z -r main
jj git push --tag vX.Y.Z
```

6. **GitHub release**

```bash
gh release create vX.Y.Z --repo jenorris/apo --title "vX.Y.Z" --latest --notes "$(cat <<'EOF'
## Apo X.Y.Z

<short why>

### Added
- …

### Changed
- …

**Upgrade:** Quit Cursor/Claude fully (Cmd+Q) if MCP schemas or tool copy changed.

See [CHANGELOG.md](https://github.com/jenorris/apo/blob/vX.Y.Z/CHANGELOG.md).
EOF
)"
```

7. **Verify**

```bash
git -C ~/Code/apo fetch origin --tags
git -C ~/Code/apo log -1 --oneline origin/main
git -C ~/Code/apo show origin/main:engine/pyproject.toml | rg '^version'
gh release view vX.Y.Z --repo jenorris/apo
```

## Docs-only follow-up (no bump)

```bash
jj new -r main
# edit docs…
jj describe -m "docs: …"
jj bookmark set main -r @
jj git push --bookmark main
```

Do **not** move `vX.Y.Z` to the new commit unless you intentionally retag.

## Optional hygiene

- Feature bookmark for the cut: `jj bookmark set feature/… -r @` then push it alongside `main` if you want a named tip.
- After upgrade, remind operators to fully quit MCP hosts so tool schemas reload.
- Keep personal/employer absolute paths out of tracked release notes.
