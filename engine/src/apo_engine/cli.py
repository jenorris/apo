"""Command-line interface: index | search | stats | watch.

This is the engine surface the Laravel MCP gateway shells out to (`apo-engine search --json`).
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import config, core


def _cmd_index(args) -> int:
    print(f"Indexing {config.NOTES_ROOT}  →  {config.INDEX_PATH}")
    s = core.index_vault(rebuild=args.rebuild, limit=args.limit)
    print(
        f"done in {s.seconds:.1f}s — "
        f"+{s.added} new, ~{s.changed} changed, -{s.removed} removed, {s.chunks} chunks embedded"
    )
    return 0


def _cmd_search(args) -> int:
    hits = core.search(args.query, k=args.k, exclude=args.exclude or None, hybrid=not args.no_hybrid)
    if args.json:
        print(json.dumps([h.__dict__ for h in hits]))
        return 0
    if not hits:
        print("(no results)")
        return 0
    for i, h in enumerate(hits, 1):
        crumb = f"  ⟩ {h.heading}" if h.heading else ""
        print(f"\n{i}. [{h.score:.3f}] {h.path}{crumb}")
        snippet = " ".join(h.text.split())
        print(f"   {snippet[:280]}{'…' if len(snippet) > 280 else ''}")
    return 0


def _cmd_stats(args) -> int:
    print(json.dumps(core.stats(), indent=2))
    return 0


def _cmd_watch(args) -> int:
    print(f"Watching {config.NOTES_ROOT} every {args.interval}s (Ctrl-C to stop)")
    try:
        while True:
            s = core.index_vault(verbose=False)
            if s.added or s.changed or s.removed:
                print(f"  reindexed: +{s.added} ~{s.changed} -{s.removed} ({s.chunks} chunks)")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="apo-engine", description="Local semantic search over a markdown vault.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("index", help="build / update the index")
    pi.add_argument("--rebuild", action="store_true", help="drop and rebuild from scratch")
    pi.add_argument("--limit", type=int, default=None, help="index only the first N notes (smoke test)")
    pi.set_defaults(func=_cmd_index)

    ps = sub.add_parser("search", help="query the index")
    ps.add_argument("query")
    ps.add_argument("-k", type=int, default=8, help="number of results")
    ps.add_argument("--exclude", nargs="*", default=[], help="glob(s) of paths to drop (e.g. 'private/*')")
    ps.add_argument("--json", action="store_true")
    ps.add_argument("--no-hybrid", action="store_true", help="vector-only (skip FTS5 BM25 fusion)")
    ps.set_defaults(func=_cmd_search)

    pt = sub.add_parser("stats", help="index stats")
    pt.set_defaults(func=_cmd_stats)

    pw = sub.add_parser("watch", help="poll the vault and reindex on change")
    pw.add_argument("--interval", type=float, default=5.0)
    pw.set_defaults(func=_cmd_watch)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
