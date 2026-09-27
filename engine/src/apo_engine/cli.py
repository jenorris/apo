"""Command-line interface: index | search | stats | doctor | watch | desk-project | serve."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__, core, ops as apo_ops, vaults
from .rpc import run_rpc
from .watch import run_watch


def _bind_cli_vault(name: str | None = None):
    default, bindings = vaults.load_bindings()
    key = (name or "").strip() or default
    if key not in bindings:
        raise SystemExit(f"unknown vault {key!r}; available: {sorted(bindings)}")
    return vaults.bind(bindings[key]), bindings[key]


def _cmd_index(args) -> int:
    vault_arg = getattr(args, "vault", None) or ""

    if getattr(args, "vacuum", False):
        cm, b = _bind_cli_vault(vault_arg)
        with cm:
            return _vacuum_index(b)

    inline_requested = bool(args.inline or args.force_inline) or (args.limit is not None)

    if inline_requested and not args.force_inline:
        watcher = apo_ops.watcher_status()
        if watcher.get("running"):
            print(
                "error: watcher is running — an in-process index here would be a second "
                "concurrent index.db writer (docs/index-concurrency.md). Pass --force-inline "
                "to override (or stop the watcher first), or drop --inline/--limit so index "
                "signals the watcher instead.",
                file=sys.stderr,
            )
            return 1

    if inline_requested:
        cm, b = _bind_cli_vault(vault_arg)
        with cm:
            print(f"[{b.name}] Indexing {vaults.notes_root()}  →  {vaults.index_path()} (inline)")
            s = core.index_vault(rebuild=args.rebuild, limit=args.limit)
            print(
                f"done in {s.seconds:.1f}s — "
                f"+{s.added} new, ~{s.changed} changed, -{s.removed} removed, {s.chunks} chunks embedded"
            )
        return 0

    result = apo_ops.reindex(
        vault=vault_arg,
        mode="rebuild",
        force=bool(args.rebuild),
        wait=args.wait,
        timeout=args.timeout,
    )
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


def _vacuum_index(b) -> int:
    """VACUUM the bound vault's index db.

    Known limitation: there is no watcher pause/resume coordination in
    deferred.py/watch.py, so this refuses outright rather than risk a lock
    conflict (or worse, vacuuming out from under an in-flight watcher write)
    — stop the watcher first.
    """
    status = apo_ops.watcher_status()
    if status.get("running"):
        print(
            f"[{b.name}] refusing --vacuum: watcher is running (pid {status.get('pid')}). "
            "Stop it first (`just watch-stop`), then retry. No pause/resume coordination "
            "exists yet, so vacuuming under a live watcher is unsafe.",
            file=sys.stderr,
        )
        return 1
    db = core.writer_connect()
    t0 = time.monotonic()
    db.execute("VACUUM")
    db.commit()
    print(f"[{b.name}] VACUUM complete in {time.monotonic() - t0:.1f}s — {vaults.index_path()}")
    return 0


def _cmd_search(args) -> int:
    vault_arg = getattr(args, "vault", None) or ""
    result = apo_ops.search(
        args.query,
        top_k=args.k,
        vault=vault_arg,
        exclude=args.exclude or None,
        hybrid=not args.no_hybrid,
    )
    if result.get("warning"):
        print(f"WARNING: {result['warning']}", file=sys.stderr)
    if args.json:
        print(json.dumps(result))
        return 0 if result.get("ok") else 1
    if not result.get("ok"):
        print(f"error: {result.get('error')}: {result.get('message')}", file=sys.stderr)
        return 1
    hits = result.get("results", [])
    if not hits:
        print("(no results)")
        return 0
    for i, h in enumerate(hits, 1):
        crumb = f"  ⟩ {h['heading']}" if h.get("heading") else ""
        print(f"\n{i}. [{h.get('score', 0):.3f}] {h.get('source', '')}{crumb}")
        snippet = " ".join((h.get("content") or "").split())
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
    vault_arg = getattr(args, "vault", None) or ""
    result = apo_ops.stats(vault=vault_arg)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "-"
    val = float(n)
    for unit in ("B", "K", "M", "G"):
        if val < 1024 or unit == "G":
            return f"{val:.0f}{unit}" if unit == "B" else f"{val:.1f}{unit}"
        val /= 1024
    return f"{val:.1f}G"


def _fmt_hook_age(iso_ts: str | None) -> str:
    """``"12s ago"`` from an ISO-8601 ``last_tick_at`` stamp, or ``"n/a"``."""
    if not iso_ts:
        return "n/a"
    import datetime as _dt

    try:
        at = _dt.datetime.fromisoformat(str(iso_ts))
    except ValueError:
        return "n/a"
    if at.tzinfo is None:
        at = at.replace(tzinfo=_dt.timezone.utc)
    age = (_dt.datetime.now(_dt.timezone.utc) - at).total_seconds()
    return f"{max(0.0, age):.0f}s ago"


def _cmd_doctor(args) -> int:
    vault = (getattr(args, "vault", None) or "").strip()
    data = apo_ops.index_health(vault=vault)
    if not data.get("ok"):
        print(json.dumps(data, indent=2))
        return 1
    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    watcher = data.get("watcher", {})
    vis = data.get("index_visibility", {})
    running = watcher.get("running")
    print(f"watcher: {'running (pid ' + str(watcher.get('pid')) + ')' if running else 'NOT RUNNING'}")
    if watcher.get("warning"):
        print(f"  warning: {watcher['warning']}")
    print(
        f"index visibility: {vis.get('path', 'n/a')}"
        + (f" (bound {vis['bound_seconds']}s)" if vis.get("bound_seconds") is not None else "")
    )
    print()

    cols = [
        ("vault", 14),
        ("db", 8),
        ("wal", 8),
        ("files", 6),
        ("chunks", 7),
        ("vec", 7),
        ("fts", 7),
        ("links", 8),
        ("lmax", 6),
        ("vorph", 6),
        ("forph", 6),
        ("quar", 5),
    ]
    header = "  ".join(f"{label:<{w}}" for label, w in cols) + "  flags"
    print(header)
    print("-" * len(header))
    exit_code = 0
    hook_lines: list[str] = []
    for name, v in data.get("vaults", {}).items():
        flags = v.get("flags") or []
        if flags:
            exit_code = 1
        row_vals = [
            name,
            _fmt_bytes(v.get("db_bytes")),
            _fmt_bytes(v.get("wal_bytes")),
            str(v.get("files", 0)),
            str(v.get("chunks", 0)),
            str(v.get("vec_chunks", 0)),
            str(v.get("fts_rows", 0)),
            str(v.get("backlinks_rows", 0)),
            str(v.get("backlinks_per_file_max", 0)),
            str(v.get("vec_chunks_orphans", 0)),
            str(v.get("fts_orphans", 0)),
            str(v.get("quarantined", 0)),
        ]
        row = "  ".join(f"{val:<{w}}" for val, (_, w) in zip(row_vals, cols))
        print(f"{row}  {','.join(flags) or '-'}")
        hooks = v.get("hooks") or {}
        if hooks:
            bits = []
            for hook_name in sorted(hooks):
                h = hooks[hook_name]
                if not isinstance(h, dict):
                    continue
                marker = "" if h.get("ok", True) else " ERROR"
                bits.append(f"{hook_name}={_fmt_hook_age(h.get('last_tick_at'))}{marker}")
            if bits:
                hook_lines.append(f"  [{name}] hooks: " + ", ".join(bits))
    for line in hook_lines:
        print(line)
    return exit_code


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
    """Render desk policy body + guidance from live desk + vault contracts."""
    from . import vault_project

    mode = (getattr(args, "mode", None) or "full").strip().lower()
    if mode not in ("full", "index"):
        print(f"error: --mode must be full|index, got {mode!r}", file=sys.stderr)
        return 1
    out = vault_project.project_live(vaults=args.vaults or None, mode=mode)
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


def _cmd_optima_merge(args) -> int:
    """Stage B: merge domain schedules → Optima current.yaml (no gws)."""
    from . import optima_merge

    name = (getattr(args, "vault", None) or "").strip() or "optima"
    cm, b = _bind_cli_vault(name)
    with cm:
        result = optima_merge.run_merge(b.root, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    elif result.get("skipped"):
        print(f"[{b.name}] optima-merge skipped: {result.get('reason')}", file=sys.stderr)
    elif result.get("ok"):
        print(
            f"[{b.name}] optima-merge: {result.get('kind')} — "
            f"{result.get('theme') or 'free'}"
            + (" (degraded)" if result.get("degraded") else "")
            + (" (dry-run)" if result.get("dry_run") else "")
        )
    else:
        print(
            f"[{b.name}] optima-merge failed: {result.get('message') or result}",
            file=sys.stderr,
        )
    return 0 if result.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="apo-engine",
        description="Local semantic search over a markdown vault.",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"apo-engine {__version__}",
    )
    vaults.add_discovery_arguments(p)
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("index", help="build / update the index (watcher-aware — see --inline)")
    pi.add_argument("--rebuild", action="store_true", help="drop and rebuild from scratch")
    pi.add_argument("--limit", type=int, default=None, help="index only the first N notes (smoke test; forces --inline)")
    pi.add_argument(
        "--vacuum",
        action="store_true",
        help="VACUUM the bound vault's index db instead of indexing; refuses if the watcher is running",
    )
    pi.add_argument(
        "--vault",
        default=os.environ.get("APO_VAULT", ""),
        help="usage-contract vault_id (default vault if empty; $APO_VAULT)",
    )
    pi.add_argument(
        "--wait",
        dest="wait",
        action="store_true",
        default=True,
        help="block until a watcher-signaled rebuild completes (default: on)",
    )
    pi.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        help="signal the watcher and return immediately instead of blocking",
    )
    pi.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="max seconds to wait for a watcher-signaled rebuild (default 30)",
    )
    pi.add_argument(
        "--inline",
        action="store_true",
        help="run the index in this process (today's direct core.index_vault behavior); "
        "refused if a watcher is live unless --force-inline",
    )
    pi.add_argument(
        "--force-inline",
        action="store_true",
        help="override the watcher-live refusal for --inline (races the watcher as a "
        "second concurrent index.db writer — see docs/index-concurrency.md)",
    )
    pi.set_defaults(func=_cmd_index)

    ps = sub.add_parser("search", help="query the index")
    ps.add_argument("query")
    ps.add_argument("-k", type=int, default=8, help="number of results")
    ps.add_argument("--exclude", nargs="*", default=[], help="glob(s) of paths to drop (e.g. 'private/*')")
    ps.add_argument("--json", action="store_true")
    ps.add_argument("--no-hybrid", action="store_true", help="keyword-only (skip vector fusion and query embed)")
    ps.add_argument("--vault", default=os.environ.get("APO_VAULT", ""), help="usage-contract vault_id ($APO_VAULT)")
    ps.set_defaults(func=_cmd_search)

    pe = sub.add_parser(
        "search-eval",
        help="labeled search-quality eval (hit@k / MRR) — file format in docs/examples/",
    )
    pe.add_argument("--file", required=True, help="YAML eval file (lives outside the repo)")
    pe.add_argument("-k", type=int, default=0, help="cutoff (default: file `k` or 5)")
    pe.add_argument("--vault", default=os.environ.get("APO_VAULT", ""), help="usage-contract vault_id ($APO_VAULT)")
    pe.add_argument("--exclude", nargs="*", default=[], help="glob(s) applied to every query")
    pe.add_argument("--json", action="store_true")
    pe.add_argument("--verbose", action="store_true", help="also list per-query passes")
    pe.set_defaults(func=_cmd_search_eval)

    pt = sub.add_parser("stats", help="index stats")
    pt.add_argument("--vault", default=os.environ.get("APO_VAULT", ""), help="usage-contract vault_id ($APO_VAULT)")
    pt.set_defaults(func=_cmd_stats)

    pdoc = sub.add_parser(
        "doctor",
        help="index introspection: db/WAL sizes, row-count parity, orphans, backlinks blowup, quarantine",
    )
    pdoc.add_argument("--vault", default="", help="usage-contract vault_id (empty = every registered vault)")
    pdoc.add_argument("--json", action="store_true")
    pdoc.set_defaults(func=_cmd_doctor)

    pw = sub.add_parser("watch", help="watch vault + consume deferred queues (sole index writer)")
    pw.add_argument("--interval", type=float, default=None, help="poll interval seconds (default from WATCH_INTERVAL)")
    pw.add_argument("--poll-only", action="store_true", help="disable fsevents; poll on interval only")
    pw.set_defaults(func=_cmd_watch)

    pd = sub.add_parser(
        "desk-project",
        help=(
            "project desk policy body + guidance from ~/.apo/desk.yaml + vault contracts "
            "(same operation as the MCP tool's vault(request={action: 'project'}))"
        ),
    )
    pd.add_argument(
        "--vaults",
        nargs="*",
        default=[],
        help="scope projection to these vault names (default: all registered)",
    )
    pd.add_argument(
        "--mode",
        choices=["full", "index"],
        default="full",
        help=(
            "full (default): complete desk body. index: compact always-loaded "
            "pointer surface — bake into a static file, call full on demand per vault."
        ),
    )
    pd.set_defaults(func=_cmd_desk_project)

    from . import okf_cli

    okf_cli.add_parser(sub)

    pr = sub.add_parser(
        "serve",
        help=(
            "DEPRECATED — legacy local JSON HTTP RPC for gateways (loopback; "
            "optional Unix socket). Prefer apo-mcp's HTTP transport (:8878) or "
            "apo-local; see docs/local-rpc.md."
        ),
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

    po = sub.add_parser(
        "optima-merge",
        help="Stage B: merge Work/Meta schedules into Optima current.yaml",
    )
    po.add_argument(
        "--vault",
        default="optima",
        help="usage-contract vault_id (default: optima)",
    )
    po.add_argument("--dry-run", action="store_true")
    po.add_argument("--json", action="store_true")
    po.set_defaults(func=_cmd_optima_merge)

    args = p.parse_args(argv)
    vaults.apply_discovery_namespace(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
