"""``apo`` — unified entry point, dispatching by leading token.

Naming-consolidation pass (2026-09): additive only. ``apo-engine``,
``apo-local``, and ``apo-mcp`` keep working exactly as they do today — this
module does not replace or change any of their behavior, it just gives a
single ``apo`` binary a way to reach all three without three names to
remember. See ``engine/pyproject.toml`` ``[project.scripts]`` for the full
naming history/rationale.

Dispatch:

  apo <note-verb> ...     -> apo_engine.cli_ops.main   (read, search, write, append,
                             patch, patch-table, graph-neighbors, filter, backlinks,
                             history — same backend as apo-local / the MCP note tools)
  apo engine <cmd> ...     -> apo_engine.cli.main        (index, search-eval, stats,
                             watch, desk-project, okf, optima-merge — same as
                             apo-engine)
  apo mcp                  -> apo_engine.mcp.server.main  (stdio/http MCP server,
                             env-configured, same as apo-mcp)

apo-engine's own ``search`` subcommand was removed (2026-09 cleanup pass):
v0.29.0 had already made it call the same ``ops.search`` apo-local's
``search`` calls (previously it called ``core.search`` directly, a simpler
single-vault query) — once both routed to the same function, keeping two
argparse front ends for it (a strict subset of flags on the apo-engine side)
was pure duplication, not a real capability split. Bare ``apo search`` (the
apo-local backend) is now the only ``search`` under this dispatcher.
``apo-engine stats``/``apo engine stats`` stay — ``ops.stats()`` (index
size/chunk counts/orphans) has no apo-local equivalent to dedupe against.

Not covered by this pass: a top-level ``vault`` or ``admin`` group mirroring
the MCP ``vault`` / ``apo_admin`` tools. Neither has a local-CLI analogue
today (that functionality is MCP-only) — inventing a new CLI surface for
them is out of scope for an additive rename/dispatch pass. They can slot in
under ``apo vault ...`` / ``apo admin ...`` once/if that functionality grows
a real CLI implementation to dispatch to.
"""
from __future__ import annotations

import sys

from . import cli as engine_cli
from . import cli_ops

# The exact set of apo-local subcommands (cli_ops.py's own `sub.add_parser`
# names) — kept as a literal tuple here (not introspected from argparse) so
# this stays a simple, obviously-correct routing table rather than reaching
# into cli_ops's private parser construction.
_NOTE_VERBS = frozenset(
    {
        "read",
        "search",
        "write",
        "append",
        "patch",
        "patch-table",
        "graph-neighbors",
        "filter",
        "backlinks",
        "history",
    }
)

_USAGE = """apo — unified Apo CLI

Usage:
  apo <note-verb> ...      note-facing ops: read, search, write, append, patch,
                           patch-table, graph-neighbors, filter, backlinks, history
                           (same backend as apo-local / the MCP note tools)
  apo engine <cmd> ...     admin/index ops: index, search-eval, stats,
                           watch, desk-project, okf, optima-merge
                           (same as apo-engine)
  apo mcp                  run the MCP server in place (same as apo-mcp;
                           stdio by default, APO_MCP_TRANSPORT=http for HTTP)

Run `apo <note-verb> --help` or `apo engine <cmd> --help` for that command's
own flags. apo-engine, apo-local, and apo-mcp still work unchanged as
separate console scripts.
"""


def _print_version() -> None:
    try:
        from . import __version__
    except ImportError:
        __version__ = "unknown"
    print(f"apo {__version__}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_USAGE)
        return 0
    if argv[0] in ("-V", "--version", "version"):
        _print_version()
        return 0

    head, rest = argv[0], argv[1:]

    if head == "engine":
        # No special-casing of an empty `rest` — apo-engine's own parser has
        # `required=True` subparsers and will produce its normal usage error
        # (or --help output), same as running `apo-engine` bare would.
        return engine_cli.main(rest)

    if head == "mcp":
        from .mcp import server as mcp_server

        mcp_server.main()
        return 0

    if head in _NOTE_VERBS:
        return cli_ops.main(argv)

    print(f"apo: unknown command {head!r} — run `apo --help`", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
