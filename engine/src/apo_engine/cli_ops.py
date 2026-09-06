"""``apo`` — vault-facing CLI mirroring the FastMCP note/vault tools.

Calls straight into :mod:`apo_engine.ops` (shared index.db, no daemon) — the
same backend ``engine/mcp/server.py``'s ``@mcp.tool`` functions call, so
behavior (concurrency guards, OKF validation, region hashes) matches the MCP
surface exactly. This is the human/shell-facing sibling of ``apo-engine``
(index/watch/serve/admin) — it exposes only the 10 vault-facing tools:
read_note, search_notes, write_note, append_note, patch_note, patch_table,
graph_neighbors, filter_notes, backlinks, history. It does NOT expose
apo_admin, scratchpad, vault (registry/desk-project), index, serve, watch,
okf, search-eval, desk-project, or optima-merge — those stay on apo-engine
or the MCP apo_admin tool.

Output default is JSON on stdout (one dict, matching the MCP tool's return
shape) so this composes in shell pipelines. ``--text``/``--oneline`` switch
to a human-readable rendering instead. Mutations (write/append/patch/
patch-table) always print a unified diff to stderr on success, independent
of output mode, so stdout stays parseable while a human still sees the
change.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from typing import Any

from . import ops as apo_ops
from . import vaults


# --------------------------------------------------------------------------- #
# Input helpers
# --------------------------------------------------------------------------- #


def _read_stdin() -> str:
    return sys.stdin.read()


def _resolve_text_arg(value: str | None) -> str | None:
    """Resolve a body/text CLI value: '-' or omitted (piped stdin) reads stdin."""
    if value == "-":
        return _read_stdin()
    if value is not None:
        return value
    if not sys.stdin.isatty():
        data = _read_stdin()
        return data if data else None
    return None


def _load_json_arg(value: str | None) -> Any:
    """Parse a JSON CLI value: literal JSON, '-' for stdin, or @path for a file."""
    if value is None:
        raw = _read_stdin()
    elif value == "-":
        raw = _read_stdin()
    elif value.startswith("@"):
        with open(value[1:], encoding="utf-8") as f:
            raw = f.read()
    else:
        raw = value
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"error: invalid JSON: {e}")


def _parse_kv_where(pairs: list[str]) -> dict[str, Any]:
    where: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"error: expected key=value, got {pair!r}")
        key, _, raw = pair.partition("=")
        key = key.strip()
        try:
            where[key] = json.loads(raw)
        except json.JSONDecodeError:
            where[key] = raw
    return where


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #


def _exit_code(result: dict[str, Any]) -> int:
    if result.get("ok"):
        return 0
    if result.get("error") == "not_found":
        return 2
    return 1


def _print_json(result: dict[str, Any]) -> None:
    print(json.dumps(result, indent=2, default=str))


def _raw_text(path: str, vault: str) -> str:
    if not path:
        return ""
    r = apo_ops.read_note(path, raw=True, vault=vault)
    return (r.get("content") or "") if r.get("ok") else ""


def _print_diff(path: str, before: str, after: str) -> None:
    if before == after:
        return
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    text = "".join(diff)
    if text:
        print(text, file=sys.stderr, end="" if text.endswith("\n") else "\n")


def _snippet(text: str, n: int = 280) -> str:
    flat = " ".join((text or "").split())
    return flat[:n] + ("…" if len(flat) > n else "")


def _render(cmd: str, result: dict[str, Any], args: argparse.Namespace) -> None:
    """Human-readable rendering for --text/--oneline. Errors always render plainly."""
    if not result.get("ok"):
        msg = result.get("message") or result.get("error") or "error"
        print(f"error: {result.get('error', 'error')}: {msg}", file=sys.stderr)
        return

    oneline = getattr(args, "oneline", False)

    if cmd == "search":
        rows = result.get("results", [])
        if oneline:
            for r in rows:
                print(f"{r.get('score', 0):.3f}\t{r.get('source', '')}\t{r.get('heading') or ''}")
        else:
            if not rows:
                print("(no results)")
            for i, r in enumerate(rows, 1):
                crumb = f"  ⟩ {r['heading']}" if r.get("heading") else ""
                print(f"\n{i}. [{r.get('score', 0):.3f}] {r.get('source', '')}{crumb}")
                print(f"   {_snippet(r.get('content') or '')}")
            if result.get("has_more"):
                print("\n(more results — pass --offset)")

    elif cmd == "filter":
        notes = result.get("notes", [])
        if oneline:
            for n in notes:
                print(f"{n.get('path', '')}\t{n.get('modified', '')}")
        else:
            if not notes:
                print("(no notes)")
            for n in notes:
                print(f"{n.get('path', '')}  ({n.get('modified', '')})")
                for k, v in (n.get("frontmatter") or {}).items():
                    print(f"    {k}: {v}")
            if result.get("has_more"):
                print("\n(more — pass --offset)")

    elif cmd == "backlinks":
        rows = result.get("backlinks", [])
        if oneline:
            for r in rows:
                print(f"{r.get('path', '')}:{r.get('line', '')}")
        else:
            if not rows:
                print("(no backlinks)")
            for r in rows:
                print(f"{r.get('path', '')}:{r.get('line', '')}  {_snippet(r.get('text') or '')}")

    elif cmd == "graph-neighbors":
        nodes = result.get("nodes", [])
        edges = result.get("edges", [])
        if oneline:
            for n in nodes:
                print(n)
        else:
            print(f"nodes ({len(nodes)}):")
            for n in nodes:
                print(f"  {n}")
            print(f"edges ({len(edges)}):")
            for e in edges:
                print(f"  {e.get('src', '')} -> {e.get('dst', '')} [{e.get('direction', '')}]")

    elif cmd == "history":
        commits = result.get("commits")
        if commits is not None:
            if oneline:
                for c in commits:
                    print(f"{c.get('hash', '')[:10]}\t{c.get('date', '')}\t{c.get('subject', '')}")
            else:
                if not commits:
                    print("(no commits)")
                for c in commits:
                    print(f"{c.get('hash', '')[:10]}  {c.get('date', '')}  {c.get('author', '')}")
                    print(f"    {c.get('subject', '')}")
        else:
            notes = result.get("notes") or []
            if oneline:
                for n in notes:
                    print(f"{n.get('path', '')}\t{n.get('modified', '')}")
            else:
                if not notes:
                    print("(no notes)")
                for n in notes:
                    print(f"{n.get('path', '')}  ({n.get('modified', '')})")

    elif cmd == "read":
        content = result.get("content")
        if content is not None:
            print(content, end="" if content.endswith("\n") else "\n")
        elif "sections" in result:
            for s in result.get("sections") or []:
                print(f"# {s.get('title', '')}")
        else:
            _print_json(result)

    elif cmd in ("write", "append", "patch", "patch-table"):
        bits = [f"ok {result.get('path', '')}"]
        if result.get("action"):
            bits.append(str(result["action"]))
        if result.get("applied") is not None:
            bits.append(f"applied={result['applied']}")
        if result.get("failed"):
            bits.append(f"failed={result['failed']}")
        if result.get("dry_run"):
            bits.append("(dry-run)")
        print(" ".join(bits))
        if result.get("warning"):
            print(f"warning: {result['warning']}", file=sys.stderr)

    else:
        _print_json(result)


def _emit(cmd: str, result: dict[str, Any], args: argparse.Namespace) -> int:
    if getattr(args, "text", False) or getattr(args, "oneline", False):
        _render(cmd, result, args)
    else:
        _print_json(result)
    return _exit_code(result)


def _diffed_mutation(path: str, vault: str, dry_run: bool, result: dict[str, Any], before: str) -> None:
    if result.get("ok") and not dry_run:
        after = _raw_text(path, vault)
        _print_diff(path, before, after)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _cmd_read(args: argparse.Namespace) -> int:
    result = apo_ops.read_note(
        args.path,
        chunk_hash=args.chunk_hash,
        heading=args.heading,
        vault=args.vault,
        start_line=args.start_line,
        end_line=args.end_line,
        max_chars=args.max_chars,
        raw=args.raw,
        force=args.force,
        fields=args.fields,
        format=args.format,
        mode=args.mode,
        sibling=args.sibling,
        siblings=args.siblings,
        lint=args.lint,
        ref=args.ref,
    )
    return _emit("read", result, args)


def _cmd_search(args: argparse.Namespace) -> int:
    result = apo_ops.search(
        args.query,
        folder=args.folder,
        folders=args.folders or None,
        vault=args.vault,
        vaults=args.vaults or None,
        snippet_chars=args.snippet_chars,
        exclude=args.exclude or None,
        limit=args.limit,
        offset=args.offset,
        ref=args.ref,
        expand=args.expand,
        intent=args.intent,
        explain=args.explain,
    )
    return _emit("search", result, args)


def _cmd_write(args: argparse.Namespace) -> int:
    content = _resolve_text_arg(args.content)
    sections = _load_json_arg(args.sections) if args.sections is not None else None
    frontmatter = _load_json_arg(args.frontmatter) if args.frontmatter is not None else None
    if content is None and sections is None and frontmatter is None:
        raise SystemExit("error: write requires --content (or stdin), --sections, or --frontmatter")
    before = _raw_text(args.path, args.vault)
    result = apo_ops.write_note(
        args.path,
        content,
        sections=sections,
        frontmatter=frontmatter,
        expected_mtime=args.expected_mtime,
        expected_frontmatter_hash=args.expected_frontmatter_hash,
        expected_body_hash=args.expected_body_hash,
        expected_content_hash=args.expected_content_hash,
        vault=args.vault,
    )
    _diffed_mutation(args.path, args.vault, False, result, before)
    return _emit("write", result, args)


def _cmd_append(args: argparse.Namespace) -> int:
    text = _resolve_text_arg(args.body if args.body is not None else args.text_pos)
    if text is None:
        raise SystemExit("error: append requires text (positional, --text, or stdin)")
    before = _raw_text(args.path, args.vault)
    result = apo_ops.append_note(
        args.path,
        text,
        heading=args.heading,
        chunk_hash=args.chunk_hash,
        position=args.position,
        create=args.create,
        expected_mtime=args.expected_mtime,
        expected_frontmatter_hash=args.expected_frontmatter_hash,
        expected_body_hash=args.expected_body_hash,
        expected_content_hash=args.expected_content_hash,
        vault=args.vault,
    )
    _diffed_mutation(args.path, args.vault, False, result, before)
    return _emit("append", result, args)


def _cmd_patch(args: argparse.Namespace) -> int:
    ops = _load_json_arg(args.ops)
    if not isinstance(ops, list):
        raise SystemExit("error: ops-json must be a JSON array of op objects")
    before = _raw_text(args.path, args.vault)
    result = apo_ops.patch_entry(
        path=args.path,
        ops=ops,
        strict=args.strict,
        dry_run=args.dry_run,
        verbose=args.verbose,
        expected_mtime=args.expected_mtime,
        expected_frontmatter_hash=args.expected_frontmatter_hash,
        expected_body_hash=args.expected_body_hash,
        expected_content_hash=args.expected_content_hash,
        vault=args.vault,
    )
    _diffed_mutation(args.path, args.vault, args.dry_run, result, before)
    return _emit("patch", result, args)


def _cmd_patch_table(args: argparse.Namespace) -> int:
    ops = _load_json_arg(args.ops)
    if not isinstance(ops, list):
        raise SystemExit("error: ops-json must be a JSON array of op objects")
    before = _raw_text(args.path, args.vault)
    result = apo_ops.patch_note(
        args.path,
        ops,
        strict=args.strict,
        dry_run=args.dry_run,
        verbose=args.verbose,
        expected_mtime=args.expected_mtime,
        expected_content_hash=args.expected_content_hash,
        vault=args.vault,
    )
    _diffed_mutation(args.path, args.vault, args.dry_run, result, before)
    return _emit("patch-table", result, args)


def _cmd_graph_neighbors(args: argparse.Namespace) -> int:
    result = apo_ops.graph_neighbors(
        args.path,
        depth=args.depth,
        direction=args.direction,
        limit=args.limit,
        offset=args.offset,
        vault=args.vault,
    )
    return _emit("graph-neighbors", result, args)


def _cmd_filter(args: argparse.Namespace) -> int:
    if args.where is not None:
        where = _load_json_arg(args.where)
    elif args.kv:
        where = _parse_kv_where(args.kv)
    else:
        where = None
    result = apo_ops.filter_notes(
        where,
        folder=args.folder,
        limit=args.limit,
        offset=args.offset,
        vault=args.vault,
        fields=args.fields,
        sort=args.sort,
        order=args.order,
        ref=args.ref,
    )
    return _emit("filter", result, args)


def _cmd_backlinks(args: argparse.Namespace) -> int:
    result = apo_ops.backlinks(args.path, limit=args.limit, offset=args.offset, vault=args.vault)
    return _emit("backlinks", result, args)


def _cmd_history(args: argparse.Namespace) -> int:
    result = apo_ops.history(
        limit=args.limit,
        offset=args.offset,
        folder=args.folder,
        path=args.path,
        vault=args.vault,
        since=args.since,
        until=args.until,
        preview=args.preview,
        heading=args.heading,
        exclude=args.exclude or None,
        fields=args.fields,
    )
    return _emit("history", result, args)


# --------------------------------------------------------------------------- #
# argparse wiring
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="apo-local",
        description=(
            "Vault-facing CLI over apo_engine.ops — same backend as the Apo MCP "
            "server's note/search tools. Admin/index/watch/serve stay on apo-engine."
        ),
    )
    vaults.add_discovery_arguments(p)
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--vault",
        default=os.environ.get("APO_VAULT", ""),
        help="registered vault_id (default: registry default, or $APO_VAULT)",
    )
    common.add_argument("--text", action="store_true", help="human-readable output instead of JSON")
    common.add_argument("--oneline", action="store_true", help="compact one-line-per-item output")

    pr = sub.add_parser("read", parents=[common], help="read a note by path or chunk_hash")
    pr.add_argument("path", nargs="?", default="", help="vault-relative path (XOR --chunk-hash)")
    pr.add_argument("--chunk-hash", default=None, help="section anchor from search hits")
    pr.add_argument("--heading", default=None, help="path mode: read only this heading's section")
    pr.add_argument("--start-line", type=int, default=None)
    pr.add_argument("--end-line", type=int, default=None)
    pr.add_argument("--max-chars", type=int, default=None)
    pr.add_argument("--raw", action="store_true", help="byte-exact file text (path mode only)")
    pr.add_argument("--force", action="store_true", help="chunk_hash mode: full section past preview threshold")
    pr.add_argument("--fields", nargs="*", default=None, help="frontmatter key projection")
    pr.add_argument("--format", default="markdown", choices=["markdown", "json", "row", "node"])
    pr.add_argument("--mode", default="auto", choices=["auto", "toc", "section"])
    pr.add_argument("--sibling", default=None, choices=["prev", "next"])
    pr.add_argument("--siblings", action="store_true")
    pr.add_argument("--lint", action="store_true")
    pr.add_argument("--ref", default="", help="git ref/branch/OID at the vault root (read-only)")
    pr.set_defaults(func=_cmd_read)

    ps = sub.add_parser("search", parents=[common], help="hybrid search (lex+vec)")
    ps.add_argument("query")
    ps.add_argument("--folder", default="")
    ps.add_argument("--folders", nargs="*", default=None, help="XOR --folder")
    ps.add_argument("--vaults", nargs="*", default=None, help="fan out across vaults; XOR --vault")
    ps.add_argument("--snippet-chars", type=int, default=240)
    ps.add_argument("--limit", type=int, default=None)
    ps.add_argument("--offset", type=int, default=0)
    ps.add_argument("--exclude", nargs="*", default=None, help="path globs to drop")
    ps.add_argument("--ref", default="", help="FTS-only search at a git tip (no embeddings)")
    ps.add_argument("--expand", action="store_true", help="RRF-fuse lex+vec sub-queries")
    ps.add_argument("--intent", default="", help="disambiguation context for --expand")
    ps.add_argument("--explain", action="store_true", help="per-hit fusion breakdown")
    ps.set_defaults(func=_cmd_search)

    pw = sub.add_parser("write", parents=[common], help="create or overwrite a note")
    pw.add_argument("path")
    pw.add_argument("--content", default=None, help="note body ('-' or omitted with piped stdin reads stdin)")
    pw.add_argument("--sections", default=None, help="JSON [{heading,content,content_type}] (XOR --content)")
    pw.add_argument("--frontmatter", default=None, help="JSON object (XOR --content)")
    pw.add_argument("--expected-mtime", type=float, default=None)
    pw.add_argument("--expected-frontmatter-hash", default=None)
    pw.add_argument("--expected-body-hash", default=None)
    pw.add_argument("--expected-content-hash", default=None)
    pw.set_defaults(func=_cmd_write)

    pa = sub.add_parser("append", parents=[common], help="append text to a note (session log / History)")
    pa.add_argument("path")
    pa.add_argument("text_pos", nargs="?", default=None, metavar="text")
    pa.add_argument("--body", dest="body", default=None, help="body to append ('-' or piped stdin reads stdin); alternative to the positional text")
    pa.add_argument("--heading", default=None)
    pa.add_argument("--chunk-hash", default=None)
    pa.add_argument("--position", default="end", choices=["end", "start"])
    pa.add_argument("--create", action="store_true", help="create the note if missing")
    pa.add_argument("--expected-mtime", type=float, default=None)
    pa.add_argument("--expected-frontmatter-hash", default=None)
    pa.add_argument("--expected-body-hash", default=None)
    pa.add_argument("--expected-content-hash", default=None)
    pa.set_defaults(func=_cmd_append)

    pp = sub.add_parser("patch", parents=[common], help="mutate frontmatter/sections (patch_note ops)")
    pp.add_argument("path")
    pp.add_argument("ops", help="JSON array of ops ('-' for stdin, '@file' for a file)")
    pp.add_argument("--strict", action="store_true")
    pp.add_argument("--dry-run", action="store_true")
    pp.add_argument("--verbose", action="store_true")
    pp.add_argument("--expected-mtime", type=float, default=None)
    pp.add_argument("--expected-frontmatter-hash", default=None)
    pp.add_argument("--expected-body-hash", default=None)
    pp.add_argument("--expected-content-hash", default=None)
    pp.set_defaults(func=_cmd_patch)

    pt = sub.add_parser("patch-table", parents=[common], help="GFM table row/cell mutators")
    pt.add_argument("path")
    pt.add_argument("ops", help="JSON array of table ops ('-' for stdin, '@file' for a file)")
    pt.add_argument("--strict", action="store_true")
    pt.add_argument("--dry-run", action="store_true")
    pt.add_argument("--verbose", action="store_true")
    pt.add_argument("--expected-mtime", type=float, default=None)
    pt.add_argument("--expected-content-hash", default=None)
    pt.set_defaults(func=_cmd_patch_table)

    pg = sub.add_parser("graph-neighbors", parents=[common], help="wiki-link graph traversal")
    pg.add_argument("path")
    pg.add_argument("--depth", type=int, default=1)
    pg.add_argument("--direction", default="both", choices=["in", "out", "both"])
    pg.add_argument("--limit", type=int, default=50)
    pg.add_argument("--offset", type=int, default=0)
    pg.set_defaults(func=_cmd_graph_neighbors)

    pf = sub.add_parser("filter", parents=[common], help="frontmatter catalog (no embeddings)")
    pf.add_argument("kv", nargs="*", help="key=value predicates (equality; JSON-decoded values)")
    pf.add_argument("--where", default=None, help="full JSON predicate object (overrides key=value args)")
    pf.add_argument("--folder", default="")
    pf.add_argument("--limit", type=int, default=20)
    pf.add_argument("--offset", type=int, default=0)
    pf.add_argument("--fields", nargs="*", default=None)
    pf.add_argument("--sort", default="mtime")
    pf.add_argument("--order", default="desc", choices=["asc", "desc"])
    pf.add_argument("--ref", default="")
    pf.set_defaults(func=_cmd_filter)

    pb = sub.add_parser("backlinks", parents=[common], help="inbound [[wiki-links]] to a path")
    pb.add_argument("path")
    pb.add_argument("--limit", type=int, default=100)
    pb.add_argument("--offset", type=int, default=0)
    pb.set_defaults(func=_cmd_backlinks)

    ph = sub.add_parser("history", parents=[common], help="browse recent notes, or git log for --path")
    ph.add_argument("--path", default="", help="file-level git history (needs an active git contract)")
    ph.add_argument("--folder", default="")
    ph.add_argument("--limit", type=int, default=10)
    ph.add_argument("--offset", type=int, default=0)
    ph.add_argument("--since", default="")
    ph.add_argument("--until", default="")
    ph.add_argument("--preview", default="first", choices=["first", "last"])
    ph.add_argument("--heading", default="")
    ph.add_argument("--exclude", nargs="*", default=None)
    ph.add_argument("--fields", nargs="*", default=None)
    ph.set_defaults(func=_cmd_history)

    args = p.parse_args(argv)
    vaults.apply_discovery_namespace(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
