"""Shared Apo toolset-routing instructions.

Single source of truth for the text handed to every Apo client: the stdio MCP
server passes it as ``FastMCP(..., instructions=...)`` in its handshake, and
the local RPC server (``rpc.py``) serves it over ``GET /v1/instructions`` for
non-MCP HTTP clients (e.g. the Hermes/Lyra memory-provider plugin) that have
no stdio transport to receive it from.
"""

from __future__ import annotations

MCP_INSTRUCTIONS = (
    "Apo: vault-relative Markdown + YAML catalog notes; sqlite-vec hybrid search; "
    "files are source of truth. "
    "apo_admin(action=list|describe|invoke): engine ops (memory_status, reindex, "
    "delete_note, reload_config, git_sync, list_refs). Destructive invoke requires "
    "confirm=true (delete_note always; reindex force=true; git_sync run/pull/rebase). "
    "vault(action=list|contracts|describe|merge|project|stats|lint|clone): registry + contracts + "
    "optional habit KPIs (stats) + archival lint (flaws[]) + clone(vault=<from>, to=<to>) scaffolds "
    "system/ (contracts/config/schemas, not content) from one already-registered vault into "
    "another; dry_run=true to preview; never overwrites an existing destination file. "
    "scratchpad(action=create|checkout|read|patch|validate|bind_schema|commit|discard|status|duplicate): "
    "ephemeral workshop buffer under ~/.apo/scratchpads/; vault-free create/patch/read; "
    "vault= required for bind_schema/commit; schemas under system/schemas/; "
    "duplicate(session_id=<source>) forks a buffer (any state, including a committed/PROMOTED "
    "known-good template) into a brand-new independent session — use for a series of variants "
    "off one template without mutating it or round-tripping the vault; "
    "promote via commit, write_note/append_note(scratchpad=session_id), or patch_note(scratchpad=session_id). "
    "Routing: write_note=create/overwrite (content=); "
    "append_note=session log / post-search add (text=); "
    "patch_note=frontmatter/section mutate or place op (move/copy; allow_cross_vault=true to copy "
    "— never move — a note into a different vault, rejected otherwise); "
    "search_notes(limit=, folder= or folders=[]); filter_notes(where=); "
    "read_note(path= or chunk_hash= from search hits); "
    "ref= on filter_notes/read_note/search_notes = read-only git tip (catalog / blob / FTS); "
    "omit ref= only when intentionally querying the indexed working tree; "
    "never pass ref= to writes. "
    "Thread mtime → expected_mtime on follow-up writes. "
    "Operator traces: otlp-mcp + Jaeger (not Apo MCP). "
    "Multi-vault: vault= or search_notes(vaults=[]); "
    "paths may be vault_id:rel (must be a vault configured on this MCP process; "
    "writes limited to that registry; responses include qualified_path)."
)
