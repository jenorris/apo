"""CLI-shaped aliases for the ``*_note`` MCP tools.

The CLI verbs are ``read``, ``search``, ``write``, ``append``, ``patch`` and
``filter``. The MCP tools carry a ``_note`` suffix. Each alias below is registered
on the same function as its canonical tool, so both names run identical code with
identical arguments and return shapes.

Aliases are not canonical tools. They are tagged ``alias``, hidden from
``tools/list`` by default (so existing clients keep the same tool surface and
schema size), and excluded from the documented tool count. Set
``APO_MCP_LIST_ALIASES=1`` to list them.
"""

from __future__ import annotations

ALIAS_TAG = "alias"
LIST_ALIASES_ENV = "APO_MCP_LIST_ALIASES"

# alias -> canonical tool name
TOOL_ALIASES: dict[str, str] = {
    "read": "read_note",
    "search": "search_notes",
    "write": "write_note",
    "append": "append_note",
    "patch": "patch_note",
    "filter": "filter_notes",
}


def canonical_tool(name: str) -> str:
    """Return the canonical tool name for ``name`` (identity for non-aliases)."""
    return TOOL_ALIASES.get(name, name)
