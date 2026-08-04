"""Command-line interface: index | search | stats | tool-stats | watch | desk-project | serve."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import core, tool_metrics, vaults
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
        degraded = core.last_search_degraded()
        if degraded:
            print(
                f"WARNING: {degraded} — results are keyword-only (BM25). "
                "Check the embed backend (`just ollama`; APO_OLLAMA_URL / APO_MODEL).",
                file=sys.stderr,
            )
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


def _cmd_search_eval(args) -> int:
    from . import search_eval

    report = search_eval.run_eval(
        args.file,
        k=args.k,
        vault=getattr(args, "vault", "") or "",
        exclude=args.exclude or None,
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(search_eval.format_report(report, verbose=args.verbose))
    return 0


def _cmd_stats(args) -> int:
    cm, b = _bind_cli_vault(getattr(args, "vault", None))
    with cm:
        data = core.stats()
        data["vault"] = b.name
        print(json.dumps(data, indent=2))
    return 0


def _cmd_tool_stats(args) -> int:
    """Roll up MCP tool-use JSONL (privacy-safe; no note bodies)."""
    days = None if args.all else args.days
    coll = (args.collection or "").strip()
    if not coll:
        _cm, b = _bind_cli_vault(getattr(args, "vault", None))
        with _cm:
            coll = b.collection
    data = tool_metrics.tool_stats(coll, days=days, tool=args.tool or None)
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


def _cmd_desk_project(args) -> int:
    """Render apo-desk Cursor/Claude artifacts from live desk + vault contracts."""
    from . import vault_project

    host = (getattr(args, "host", None) or "both").strip() or "both"
    write = not bool(getattr(args, "dry_run", False))
    if write:
        out = vault_project.project_live(host=host, write=True)
    else:
        out = vault_project.project_live(host=host, write=False)
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


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

    pe = sub.add_parser(
        "search-eval",
        help="labeled search-quality eval (hit@k / MRR) — file format in docs/examples/",
    )
    pe.add_argument("--file", required=True, help="YAML eval file (lives outside the repo)")
    pe.add_argument("-k", type=int, default=0, help="cutoff (default: file `k` or 5)")
    pe.add_argument("--vault", default="", help="vault name from APO_VAULTS")
    pe.add_argument("--exclude", nargs="*", default=[], help="glob(s) applied to every query")
    pe.add_argument("--json", action="store_true")
    pe.add_argument("--verbose", action="store_true", help="also list per-query passes")
    pe.set_defaults(func=_cmd_search_eval)

    pt = sub.add_parser("stats", help="index stats")
    pt.add_argument("--vault", default="", help="vault name from APO_VAULTS")
    pt.set_defaults(func=_cmd_stats)

    pts = sub.add_parser(
        "tool-stats",
        help="MCP tool-use rollups from ~/.apo/tool-metrics-*.jsonl",
    )
    pts.add_argument("--days", type=int, default=7, help="rollup window (default 7)")
    pts.add_argument("--all", action="store_true", help="include all events (ignore --days)")
    pts.add_argument("--tool", default="", help="optional tool name filter")
    pts.add_argument("--vault", default="", help="vault name (resolves collection)")
    pts.add_argument(
        "--collection",
        default="",
        help="metrics collection id (default: vault collection)",
    )
    pts.set_defaults(func=_cmd_tool_stats)

    pw = sub.add_parser("watch", help="watch vault + consume deferred queues (sole index writer)")
    pw.add_argument("--interval", type=float, default=None, help="poll interval seconds (default from WATCH_INTERVAL)")
    pw.add_argument("--poll-only", action="store_true", help="disable fsevents; poll on interval only")
    pw.set_defaults(func=_cmd_watch)

    pd = sub.add_parser(
        "desk-project",
        help="project apo-desk Cursor/Claude/Hermes artifacts from ~/.apo/desk.yaml + vault contracts",
    )
    pd.add_argument(
        "--host",
        default="both",
        help="cursor | claude | hermes | both (cursor+claude) | all (default both)",
    )
    pd.add_argument(
        "--dry-run",
        action="store_true",
        help="return rendered text without writing host files",
    )
    pd.set_defaults(func=_cmd_desk_project)

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
