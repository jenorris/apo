# apo — thin MCP client

A single static binary (Go, ~8MB, no runtime dependency) that speaks MCP
streamable-HTTP to a *running* apo-engine server. No local vault access, no
engine dependencies (duckdb, sqlite-vec, watchdog, ...) — every command is
one MCP tool call over the wire. Pairs with the containerized engine (see
`../Dockerfile`) so you can run the engine anywhere and talk to it from
anywhere on the network, with nothing heavier than one binary installed on
the client side.

For the same commands run in-process against a local vault, with no server
required, see `apo-local` (`apo_engine.cli_ops`, part of the `apo-engine`
Python package) instead.

## Install

```bash
cd client
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w -X main.version=$(git describe --tags --always)" -o /usr/local/bin/apo .
```

Cross-compile for another platform by setting `GOOS`/`GOARCH` (e.g.
`GOOS=darwin GOARCH=arm64`) before `go build` — no other changes needed.

## Usage

```bash
export APO_SERVER_URL=http://127.0.0.1:8878/mcp   # or --server, per-invocation

apo tools                                          # list what the server exposes
apo search "kitchen lights" --folder areas --limit 5
apo read areas/threads/office-hvac.md
apo call write_note --args '{"path":"areas/threads/x.md","content":"..."}'
```

`tools`/`search`/`read` cover the common cases with a couple of hand-mapped
flags for ergonomics. Everything the server exposes — including tools added
after this binary was built — is reachable through `call <tool> --args
'<json>'`, which is deliberately the source of truth: this client never goes
stale as the server's tool schemas evolve, because it doesn't hardcode them.
`apo call --args` reads stdin when `--args` is omitted, so it composes:

```bash
echo '{"path":"areas/threads/x.md","ops":[{"op":"set_field","field":"status","value":"done"}]}' \
  | apo call patch_note
```

### Piping a command's output into a note

`apo append <path>` wraps `append_note` for the common case of capturing a
command's output directly — stdin is the note *text*, not a JSON envelope,
so it composes with anything:

```bash
journalctl --user -u comfyui.service -n 50 | apo append changelog/2026-09-08-wedge-notes.md
some-report --json | jq . | apo append projects/x/latest-run.md --heading "Latest run"
```

`create` defaults to `false` (matches `append_note`): a typo'd path errors
instead of silently creating a note. Pass `--create` to create-on-first-use.
**Caveat:** even with `--create`, a leading YAML block in the piped text is
**not** parsed as frontmatter — it lands as literal body text under an
auto-stamped minimal frontmatter (derived title only). Set real frontmatter
separately (`apo call write_note`/`patch_note`) if it matters for that note.

## Auth

`--token` / `APO_TOKEN` sets a bearer token on every request — for an
`APO_MCP_AUTH=google`-gated instance (see
`../engine/docs/systemd/apo-desma-mcp.service.example`), pass whatever OIDC
ID token that deployment's flow produces. Unset for the common case: a
loopback or tailnet-only server with no auth configured.

## Design notes

- **Official SDK, not hand-rolled protocol.** Uses
  `github.com/modelcontextprotocol/go-sdk`, maintained in collaboration with
  Google — reduces the risk of protocol drift as the MCP spec evolves,
  versus reimplementing streamable-HTTP JSON-RPC framing by hand.
- **No CLI framework.** Flag parsing is a small hand-rolled partitioner (see
  `partition` in `main.go`) rather than a dependency like cobra — stdlib's
  own `flag` package can't handle a flag placed after a positional argument
  (`apo call write_note --args '...'` breaks with it), and pulling in a
  framework to fix that would cut against the whole point of this being the
  lightest possible binary. Both `--name value` and `--name=value` are
  accepted for every flag, in any position — an agent generating the
  command line has no reason to know this binary's specific quirks, so
  neither form should ever silently misbehave.
- **Structured-content first.** `apo-engine`'s MCP tools return JSON-shaped
  results; this client prefers `CallToolResult.StructuredContent` when the
  server provides it and falls back to parsing concatenated `TextContent` as
  JSON otherwise, so output stays a single clean JSON value either way.
