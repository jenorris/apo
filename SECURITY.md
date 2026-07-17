# Security policy

Apo is a local-first tool: it reads and writes Markdown files on your own
machine and builds a rebuildable sqlite-vec index. It does not phone home,
require accounts, or transmit your notes anywhere. Embeddings are computed
by a local Ollama daemon (or a local ONNX model).

## What to keep private

- **Your index (`index.db`)** contains embeddings of your notes. It is
  git-ignored by default — keep it that way.
- **Your `.env` / `config.env`** hold absolute vault paths. These are
  git-ignored; only `config.env.example` is tracked.

## Reporting a vulnerability

If you find a security issue in the engine or MCP server, please open a
GitHub security advisory (Security → Report a vulnerability) or a private
issue rather than a public one, and allow reasonable time for a fix before
disclosure.

Because Apo runs entirely on the operator's machine against their own files,
the realistic threat surface is: path traversal outside the configured vault
root, unsafe handling of untrusted note content during indexing, or the MCP
server exposing more than the intended tool set. Reports in those areas are
especially welcome.
