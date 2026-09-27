# Local RPC (removed)

The hand-rolled JSON-HTTP RPC server (`apo-engine serve`,
`engine/src/apo_engine/rpc.py`) was **deleted 2026-09-27**. It had been frozen
and deprecated since v0.28.3 (12,030 failed production requests logged, 0
successes) and had no remaining consumers by the time of removal — its one
confirmed real caller migrated to `apo-local write` directly, and the
known-but-unverified `apo-enterprise` Laravel gateway had no evidence of use.
Deleting it also let the test suite drop 8 `ThreadingHTTPServer`-backed test
classes that only re-exercised `ops.py` behavior through an HTTP costume;
that coverage now lives as direct `ops.*` tests.

To read the removed surface as-is (endpoint table, env vars, the
`apo-enterprise` notes), check out this file at the last commit before
removal: `git show b9e6be6:docs/local-rpc.md` (apo repo, `main`).

For new work, use:
- **`apo-mcp`'s HTTP transport** (FastMCP, default `:8878`) — the same tool
  surface as stdio MCP, over HTTP, plus the Go `apo-remote` client as a ready
  CLI.
- **`apo-local`** (or the unified `apo` bare-verb dispatch) for in-process
  calls with no daemon at all.
