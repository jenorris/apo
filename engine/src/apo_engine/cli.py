"""Command-line interface: index | search | stats | watch | serve."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import core, vaults
from .rpc import run_rpc
from .watch import run_watch


def _bind_cli_vault(name: str | None = None):
    default, bindings = vaults.load_bindings()
    key = (name or "").strip() or default
    if key not in bindings:
        raise SystemExit(f"unknown vault {key!r}; available: {sorted(bindings)}")
    return vaults.bind(bindings[key]), bindings[key]


def _cmd_index(args) -> int:
    cm, b = _bind_cli_vault(getattr(args, "vault", None))
    with cm:
        print(f"[{b.name}] Indexing {vaults.notes_root()}  →  {vaults.index_path()}")
        s = core.index_vault(rebuild=args.rebuild, limit=args.limit)
        print(
            f"done in {s.seconds:.1f}s — "
            f"+{s.added} new, ~{s.changed} changed, -{s.removed} removed, {s.chunks} chunks embedded"
        )
    return 0


def _cmd_search(args) -> int:
    cm, b = _bind_cli_vault(getattr(args, "vault", None))
    with cm:
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
    cm, b = _bind_cli_vault(getattr(args, "vault", None))
    with cm:
        data = core.stats()
        data["vault"] = b.name
        print(json.dumps(data, indent=2))
    return 0


def _cmd_watch(args) -> int:
    run_watch(interval=args.interval, use_events=not args.poll_only, verbose=True)
    return 0


def _cmd_serve(args) -> int:
    host = args.host or os.environ.get("APO_RPC_HOST", "127.0.0.1")
    port = args.port if args.port else int(os.environ.get("APO_RPC_PORT", "8765"))
    sock = (args.socket or os.environ.get("APO_RPC_SOCKET", "")).strip() or None
    if args.token is not None:
        token = args.token
    else:
        token = os.environ.get("APO_RPC_TOKEN", "")
    run_rpc(host=host, port=port, socket_path=sock, token=token or None)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="apo-engine", description="Local semantic search over a markdown vault.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("index", help="build / update the index")
    pi.add_argument("--rebuild", action="store_true", help="drop and rebuild from scratch")
    pi.add_argument("--limit", type=int, default=None, help="index only the first N notes (smoke test)")
    pi.add_argument("--vault", default="", help="vault name from APO_VAULTS (default vault if empty)")
    pi.set_defaults(func=_cmd_index)

    ps = sub.add_parser("search", help="query the index")
    ps.add_argument("query")
    ps.add_argument("-k", type=int, default=8, help="number of results")
    ps.add_argument("--exclude", nargs="*", default=[], help="glob(s) of paths to drop (e.g. 'private/*')")
    ps.add_argument("--json", action="store_true")
    ps.add_argument("--no-hybrid", action="store_true", help="vector-only (skip FTS5 BM25 fusion)")
    ps.add_argument("--vault", default="", help="vault name from APO_VAULTS")
    ps.set_defaults(func=_cmd_search)

    pt = sub.add_parser("stats", help="index stats")
    pt.add_argument("--vault", default="", help="vault name from APO_VAULTS")
    pt.set_defaults(func=_cmd_stats)

    pw = sub.add_parser("watch", help="watch vault + consume deferred queues (sole index writer)")
    pw.add_argument("--interval", type=float, default=None, help="poll interval seconds (default from WATCH_INTERVAL)")
    pw.add_argument("--poll-only", action="store_true", help="disable fsevents; poll on interval only")
    pw.set_defaults(func=_cmd_watch)

    pr = sub.add_parser(
        "serve",
        help="local JSON HTTP RPC for gateways (loopback; optional Unix socket)",
    )
    pr.add_argument("--host", default="", help="bind host (default APO_RPC_HOST or 127.0.0.1)")
    pr.add_argument("--port", type=int, default=0, help="bind port (default APO_RPC_PORT or 8765)")
    pr.add_argument(
        "--socket",
        default="",
        help="Unix domain socket path (APO_RPC_SOCKET); overrides host/port when set",
    )
    pr.add_argument(
        "--token",
        default=None,
        help="optional bearer token (default APO_RPC_TOKEN; empty = no auth on loopback)",
    )
    pr.set_defaults(func=_cmd_serve)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
