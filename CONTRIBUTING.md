# Contributing

Apo is a small, local-first project. Contributions are welcome — bug reports,
fixes, and focused features.

## Setup

```bash
git clone https://github.com/jenorris/apo.git ~/Code/apo
cd ~/Code/apo
cp config.env.example .env      # edit APO_NOTES_ROOT to point at a test vault
just setup                      # engine venv + MCP extras (needs Python 3.11+)
just ollama && ollama pull bge-m3
```

See [docs/quickstart.md](docs/quickstart.md) for the full install and MCP
registration flow.

## Tests

```bash
just test            # or: cd engine && .venv/bin/python -m pytest tests
```

`just setup` installs pytest via the `dev` extra. Tests are hermetic — they must
never write to `~/.apo` or a real vault (see `engine/tests/conftest.py`); CI runs
the suite on Linux and macOS for every push and PR.

Please add or update tests for behavior changes. The MCP tool surface has a
lean/full contract covered by `engine/tests/test_apo_admin.py` — keep it green.

## Ground rules

- **Files are the source of truth.** The index is disposable; never make the
  engine depend on index state that can't be rebuilt from Markdown.
- **Single writer.** Only the watcher writes `index.db`. The MCP server reads
  and enqueues — see [docs/index-concurrency.md](docs/index-concurrency.md).
- **Convention-agnostic engine.** Vault layout, frontmatter schemas, and PARA/
  OKF/wiki habits are user policy (vault **contracts**, or a template under docs/contracts/), not engine
  requirements. Don't hardcode a layout into the engine.
- **No personal or employer paths** in tracked files, examples, or defaults.

## Pull requests

Keep PRs focused. Describe the change, how you tested it, and any config or
migration impact.

## Releases

See **[VERSIONING.md](VERSIONING.md)** for semver rules, the jj cut checklist
(`CHANGELOG` → version bump → `main` → tag → `gh release`), and docs-only
follow-ups that skip a bump.
