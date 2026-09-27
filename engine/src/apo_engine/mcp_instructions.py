"""Shared Apo toolset-routing instructions.

Single source of truth for the text handed to every Apo client: the stdio MCP
server passes it as ``FastMCP(..., instructions=...)`` in its handshake; the
same server's HTTP transport (default ``:8878``) hands it over on connect too.
"""

from __future__ import annotations

MCP_INSTRUCTIONS = (
    "Apo: vault Markdown/YAML notes; sqlite-vec hybrid search; files are SoT. "
    "apo_admin(action=list|describe|invoke): engine ops — confirm=true when destructive. "
    "vault(request={action: list|contracts|describe|merge|project|stats|lint, …}): registry + "
    "contracts; project loads per-vault write policy; stats = 7d habit KPIs (folder, mtime). "
    "scratchpad(request={action: create|read|patch|commit|discard, …}): JSON/YAML buffer; "
    "commit needs vault= + path. "
    "Routing: write_note(content=); append_note(text=); "
    "patch_note(FM/section/place ops); patch_table(GFM row ops); "
    "search_notes(folder=|folders=[]); filter_notes(where=); "
    "read_note(path|chunk_hash=). ref= = read-only git tip; never on writes. "
    "Thread mtime → expected_mtime. Multi-vault: vault= or search_notes(vaults=[]); "
    "paths vault_id:rel when configured."
)
