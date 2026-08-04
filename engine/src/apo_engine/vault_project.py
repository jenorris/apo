"""Project desk skill/rule markdown from ``vault(action=merge)`` IR.

Deterministic — no LLM. Hosts: ``cursor`` (.mdc alwaysApply), ``claude`` /
``hermes`` (SKILL.md). ``both`` = cursor+claude; ``all`` = cursor+claude+hermes.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from . import vault_contracts

DEFAULT_CURSOR_OUT = Path.home() / ".cursor" / "rules" / "apo-desk.mdc"
DEFAULT_CLAUDE_OUT = Path.home() / ".claude" / "skills" / "apo-desk" / "SKILL.md"
DEFAULT_HERMES_OUT = (
    Path.home() / ".apo" / "projected" / "hermes" / "apo-desk" / "SKILL.md"
)
DEFAULT_PROJECTED_DIR = Path.home() / ".apo" / "projected"

_HOSTS = frozenset({"cursor", "claude", "hermes", "both", "all"})
_HOST_HELP = "cursor|claude|hermes|both|all"

# Watch / multi-caller debounce for auto-reproject.
_reproject_lock = threading.Lock()
_last_reproject_mono = 0.0
_last_desk_mtime: float | None = None
_last_contracts_sig: str | None = None
_MIN_REPROJECT_GAP_S = 2.0


def resolve_out_path(host: str) -> Path:
    if host == "cursor":
        explicit = os.environ.get("APO_PROJECT_CURSOR", "").strip()
        return Path(explicit).expanduser() if explicit else DEFAULT_CURSOR_OUT
    if host == "claude":
        explicit = os.environ.get("APO_PROJECT_CLAUDE", "").strip()
        return Path(explicit).expanduser() if explicit else DEFAULT_CLAUDE_OUT
    if host == "hermes":
        explicit = os.environ.get("APO_PROJECT_HERMES", "").strip()
        return Path(explicit).expanduser() if explicit else DEFAULT_HERMES_OUT
    raise ValueError(f"unknown host {host!r}")


def _abs_pointer(raw: str, vaults: dict[str, Any]) -> str:
    """Expand ``meta:path/to.md`` using merge vault roots; else return as-is."""
    text = (raw or "").strip()
    if not text or ":" not in text:
        return text
    # Avoid treating https: as vault
    if text.startswith(("http://", "https://", "/")):
        return text
    vault_id, _, rel = text.partition(":")
    row = vaults.get(vault_id)
    if not isinstance(row, dict):
        return text
    root = str(row.get("root") or "").rstrip("/")
    rel = rel.lstrip("/")
    if not root:
        return text
    if not rel.endswith(".md"):
        rel = f"{rel}.md"
    return f"{root}/{rel}"


def _md_link(label: str, path: str) -> str:
    if path.startswith("/"):
        return f"[{label}]({path})"
    return f"{label}: `{path}`"


def _usage_contribution(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return usage-contract ``contribution`` mapping when present."""
    contracts = row.get("contracts") if isinstance(row.get("contracts"), dict) else {}
    entry = contracts.get("usage-contract")
    if not isinstance(entry, dict) or not entry.get("ok", True):
        return None
    data = entry.get("data")
    if not isinstance(data, dict):
        return None
    contrib = data.get("contribution")
    return contrib if isinstance(contrib, dict) else None


def format_contribution_line(name: str, contrib: dict[str, Any]) -> str:
    """One-liner for apo-desk Contribution section (deterministic)."""
    dialect = str(contrib.get("dialect") or "gfm").strip() or "gfm"
    extras: list[str] = []

    features = contrib.get("features") if isinstance(contrib.get("features"), dict) else {}
    if features.get("callouts") is not None:
        extras.append(f"callouts {features['callouts']}")

    surfaces = contrib.get("surfaces")
    if isinstance(surfaces, dict):
        for sname, srow in sorted(surfaces.items()):
            if not isinstance(srow, dict):
                continue
            if srow.get("dialect"):
                extras.append(f"{sname}={srow['dialect']}")
            elif srow.get("callouts") is not None and srow["callouts"] != features.get(
                "callouts"
            ):
                extras.append(f"{sname}=callouts {srow['callouts']}")

    render = contrib.get("render")
    if isinstance(render, dict):
        profile = str(render.get("profile") or "").strip()
        if profile:
            extras.append(f"render `{profile}`")

    if extras:
        return f"- `{name}`: `{dialect}` ({'; '.join(extras)})"
    return f"- `{name}`: `{dialect}`"

def attach_usage_contribution_bodies(merge: dict[str, Any]) -> dict[str, Any]:
    """Ensure each vault's usage-contract entry includes parsed ``data`` for project.

    Other contracts stay summary-only. Mutates and returns ``merge``.
    """
    vaults = merge.get("vaults")
    if not isinstance(vaults, dict):
        return merge
    for _name, row in vaults.items():
        if not isinstance(row, dict):
            continue
        root_s = str(row.get("root") or "").strip()
        if not root_s:
            continue
        found = vault_contracts.discover_contracts(Path(root_s))
        usage = found.get("usage-contract")
        if not usage:
            continue
        contracts = row.get("contracts")
        if not isinstance(contracts, dict):
            contracts = {}
            row["contracts"] = contracts
        # Keep summary fields; attach full entry (with data) for usage only.
        contracts["usage-contract"] = usage
    return merge


def render_desk_body(merge: dict[str, Any]) -> str:
    """Shared markdown body (no host frontmatter)."""
    desk = merge.get("desk") if isinstance(merge.get("desk"), dict) else {}
    vaults = merge.get("vaults") if isinstance(merge.get("vaults"), dict) else {}
    default = str(merge.get("default_vault") or "")
    dual = desk.get("dual_write") if isinstance(desk.get("dual_write"), dict) else {}
    habits = desk.get("habits") if isinstance(desk.get("habits"), dict) else {}
    pointers = desk.get("pointers") if isinstance(desk.get("pointers"), dict) else {}
    citations = str(desk.get("citations") or "absolute_markdown")
    workspace = str(desk.get("workspace") or "").strip()

    lines: list[str] = []
    lines.append("# Apo desk (generated)")
    lines.append("")
    lines.append(
        "Generated by `vault(action=project)`. Source: `~/.apo/desk.yaml` + `APO_VAULTS` "
        "+ per-vault `system/contracts/`. **Do not hand-edit** — re-run `just desk-project` "
        "(or `vault(action=project, write=true)`; watcher auto-reprojects on desk/contract changes)."
    )
    lines.append("")
    lines.append("Engine API / throughput / diagnose: skill **`mcp-apo`** (stable product skill).")
    lines.append("")
    if workspace:
        lines.append(f"Desk workspace: `{workspace}`.")
        lines.append("")

    lines.append("## Desk vaults")
    lines.append("")
    lines.append("| Vault | Role | Default | Root | Contracts |")
    lines.append("|-------|------|---------|------|-----------|")
    for name, row in sorted(vaults.items()):
        if not isinstance(row, dict):
            continue
        role = row.get("role") or "—"
        is_def = "yes" if row.get("default") or name == default else ""
        root = row.get("root") or ""
        ids = row.get("contract_ids") or []
        id_s = ", ".join(ids) if ids else "—"
        lines.append(f"| `{name}` | {role} | {is_def} | `{root}` | {id_s} |")
    lines.append("")
    lines.append("Pass `vault=` for non-default. Never cross-pollinate OKF/layout between vaults.")
    lines.append("")

    contrib_lines: list[str] = []
    contrib_pointer_raws: list[str] = []
    for name, row in sorted(vaults.items()):
        if not isinstance(row, dict):
            continue
        contrib = _usage_contribution(row)
        if not contrib:
            continue
        contrib_lines.append(format_contribution_line(name, contrib))
        ptrs = contrib.get("pointers")
        if isinstance(ptrs, list):
            for p in ptrs:
                if isinstance(p, str) and p.strip():
                    contrib_pointer_raws.append(p.strip())
        render = contrib.get("render")
        if isinstance(render, dict):
            rp = render.get("pointer")
            if isinstance(rp, str) and rp.strip():
                contrib_pointer_raws.append(rp.strip())
    if contrib_lines:
        lines.append("## Contribution")
        lines.append("")
        lines.append(
            "Per-vault authoring dialect from usage-contract "
            "(`plain-md` | `gfm` | `obsidian-ofm`). "
            "Render profiles (`htmlize`) are export-only — not body syntax."
        )
        lines.append("")
        lines.extend(contrib_lines)
        lines.append("")

    lines.append("## Dual-write")
    lines.append("")
    sv = dual.get("session_vault") or "sessions"
    sp = dual.get("session_path_template") or "inbox/daily/{date}.md"
    sh = dual.get("session_heading") or "Session log"
    domain = dual.get("domain_vaults")
    if isinstance(domain, list) and domain:
        domain_s = ", ".join(f"`{d}`" for d in domain)
    else:
        # Default: all non-session / non-audit roles
        domain_s = ", ".join(
            f"`{n}`"
            for n, r in sorted(vaults.items())
            if isinstance(r, dict) and r.get("role") not in {"audit", None}
            and n != sv
        ) or "`meta` / `norris` / `work` / `contracts`"
    lines.append(
        f"On consequential turns (and new durable facts when enabled): domain note in the owning vault "
        f"({domain_s}); **always** `append_note(..., vault=\"{sv}\")` on `{sp}` → `## {sh}` "
        f"with `YYYY-MM-DD HH:MM ET` (process turns: `Tooling:`). "
        f"Never write session logs to Meta/Norris/Work/Contracts dailies."
    )
    if habits.get("end_of_turn_gate", True):
        lines.append("")
        lines.append("End-of-turn gate: do not reply until required writes land.")
    lines.append("")

    lines.append("## Citations")
    lines.append("")
    if citations == "absolute_markdown":
        lines.append(
            "Chat citations: absolute markdown links ending in `.md` "
            "(Cursor does not resolve `[[wiki-links]]`). Keep wiki-links inside vault note bodies / `related:`."
        )
    else:
        lines.append(f"Citation mode: `{citations}`.")
    lines.append("")

    if habits:
        lines.append("## Habits")
        lines.append("")
        if habits.get("new_durable_facts", True):
            lines.append(
                "- Capture **new durable facts** when learned (dates, people, equipment, schedule, preferences, decisions) — do not defer to end of turn."
            )
        if habits.get("prefer_append_patch", True):
            lines.append("- Prefer `append_note` / `patch_note` over full-file rewrites; archive via `place_note`.")
        if habits.get("filter_okf_type", True):
            lines.append(
                "- When a vault has an OKF contract: stamp `okf_type` / `description` / `timestamp` on concept writes; prefer `filter_notes({\"okf_type\": \"…\"}, folder=…)`."
            )
        lines.append("")

    lines.append("## Contract inventory")
    lines.append("")
    lines.append("Live machine IR under each vault's `system/contracts/` (discovered by `vault(action=contracts|merge)`).")
    lines.append("")
    for name, row in sorted(vaults.items()):
        if not isinstance(row, dict):
            continue
        contracts = row.get("contracts") if isinstance(row.get("contracts"), dict) else {}
        if not contracts:
            lines.append(f"- `{name}`: _(none)_")
            continue
        bits = []
        for cid, entry in sorted(contracts.items()):
            if isinstance(entry, dict):
                bits.append(f"`{cid}` ← `{entry.get('path')}`")
            else:
                bits.append(f"`{cid}`")
        lines.append(f"- `{name}`: " + "; ".join(bits))
    lines.append("")

    # Role-specific short notes without dumping Meta OKF
    role_notes = desk.get("role_notes") if isinstance(desk.get("role_notes"), dict) else {}
    if role_notes:
        lines.append("## Role notes")
        lines.append("")
        for role, note in sorted(role_notes.items()):
            lines.append(f"- **{role}:** {note}")
        lines.append("")

    if pointers:
        lines.append("## Deep policy pointers")
        lines.append("")
        seen_ptr: set[str] = set()
        for key, raw in sorted(pointers.items()):
            path = _abs_pointer(str(raw), vaults)
            if path in seen_ptr:
                continue
            seen_ptr.add(path)
            label = key.replace("_", " ")
            lines.append(f"- {_md_link(label, path)}")
        for raw in contrib_pointer_raws:
            path = _abs_pointer(raw, vaults)
            if path in seen_ptr:
                continue
            seen_ptr.add(path)
            # vault_id:rel → short label from basename
            label = Path(path).stem.replace("-", " ").replace("_", " ")
            lines.append(f"- {_md_link(label, path)}")
        lines.append("")
    elif contrib_pointer_raws:
        lines.append("## Deep policy pointers")
        lines.append("")
        seen_ptr: set[str] = set()
        for raw in contrib_pointer_raws:
            path = _abs_pointer(raw, vaults)
            if path in seen_ptr:
                continue
            seen_ptr.add(path)
            label = Path(path).stem.replace("-", " ").replace("_", " ")
            lines.append(f"- {_md_link(label, path)}")
        lines.append("")

    lines.append("## Safety")
    lines.append("")
    lines.append("- `delete_note` only with explicit confirmation (admin / non-lean).")
    lines.append("- Do not substitute Apo for repo Grep/Glob or hosted Confluence/Jira/Slack/Gmail/Calendar.")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_cursor_mdc(merge: dict[str, Any]) -> str:
    body = render_desk_body(merge)
    header = (
        "---\n"
        "description: Apo desk policy (generated from vault merge) — do not hand-edit\n"
        "alwaysApply: true\n"
        "---\n\n"
    )
    return header + body


def render_claude_skill(merge: dict[str, Any]) -> str:
    body = render_desk_body(merge)
    header = (
        "---\n"
        "name: apo-desk\n"
        "description: >-\n"
        "  Apo multi-vault desk policy (generated from vault merge). Vault table,\n"
        "  dual-write, citations, contract pointers. Use with mcp-apo for tool routing.\n"
        "---\n\n"
    )
    return header + body


def render_hermes_skill(merge: dict[str, Any]) -> str:
    """Hermes / Lyra native skill — same desk body as Claude, Hermes frontmatter."""
    body = render_desk_body(merge)
    header = (
        "---\n"
        "name: apo-desk\n"
        "description: >-\n"
        "  Apo multi-vault desk policy (generated from vault merge). Durable PARA via\n"
        "  Apo MCP/RPC; keep Mnemosyne as episodic memory — do not displace it.\n"
        "---\n\n"
    )
    return header + body


def _render_for_host(host: str, merge: dict[str, Any]) -> str:
    if host == "cursor":
        return render_cursor_mdc(merge)
    if host == "claude":
        return render_claude_skill(merge)
    if host == "hermes":
        return render_hermes_skill(merge)
    raise ValueError(f"unknown host {host!r}")


def _project_targets(host: str) -> list[str]:
    if host == "both":
        return ["cursor", "claude"]
    if host == "all":
        return ["cursor", "claude", "hermes"]
    return [host]


def project(
    merge: dict[str, Any],
    *,
    host: str = "both",
    write: bool = False,
) -> dict[str, Any]:
    """Render (and optionally write) host projections from a merge payload."""
    h = (host or "both").strip().lower()
    if h not in _HOSTS:
        return {
            "ok": False,
            "error": "bad_host",
            "message": f"host must be {_HOST_HELP}",
        }
    targets = _project_targets(h)
    out: dict[str, Any] = {"ok": True, "action": "project", "host": h, "files": {}}
    for t in targets:
        text = _render_for_host(t, merge)
        path = resolve_out_path(t)
        entry: dict[str, Any] = {
            "path": str(path),
            "bytes": len(text.encode("utf-8")),
            "written": False,
        }
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            entry["written"] = True
            # Canonical copy under ~/.apo/projected/
            projected = DEFAULT_PROJECTED_DIR
            projected.mkdir(parents=True, exist_ok=True)
            if t == "cursor":
                name = "apo-desk.mdc"
            elif t == "hermes":
                name = "hermes-apo-desk.SKILL.md"
            else:
                name = "apo-desk.SKILL.md"
            (projected / name).write_text(text, encoding="utf-8")
            entry["projected_copy"] = str(projected / name)
        else:
            entry["text"] = text
        out["files"][t] = entry
    return out


def _desk_mtime() -> float | None:
    from . import vault_desk

    path = vault_desk.resolve_desk_path()
    if path is None or not path.is_file():
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _contracts_signature() -> str:
    """Cheap fingerprint of live contract files across APO_VAULTS."""
    from . import vaults

    try:
        _default, bindings = vaults.load_bindings()
    except Exception:
        return ""
    parts: list[str] = []
    for name, b in sorted(bindings.items()):
        root = b.resolved().root
        cdir = root / "system" / "contracts"
        if not cdir.is_dir():
            continue
        try:
            for path in sorted(cdir.iterdir()):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in {".yaml", ".yml"}:
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                parts.append(f"{name}:{path.name}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            continue
    return "|".join(parts)


def is_contracts_rel(rel: str) -> bool:
    """True when a vault-relative path is under ``system/contracts/``."""
    r = (rel or "").replace("\\", "/").lstrip("/")
    return r == "system/contracts" or r.startswith("system/contracts/")


def project_live(*, host: str = "both", write: bool = True) -> dict[str, Any]:
    """Build merge IR from the live registry/desk and project (CLI / watch)."""
    from . import ops

    return ops.vault_op("project", host=host, write=write)


def maybe_reproject(
    *,
    reason: str = "",
    force: bool = False,
    verbose: bool = False,
) -> dict[str, Any] | None:
    """Write apo-desk when desk.yaml or ``system/contracts/`` change.

    Debounced across vault watcher threads. Returns the project result when a
    write runs, else ``None`` (skipped).
    """
    global _last_reproject_mono, _last_desk_mtime, _last_contracts_sig

    with _reproject_lock:
        now = time.monotonic()
        desk_mt = _desk_mtime()
        sig = _contracts_signature()
        changed = force
        if not changed:
            if desk_mt is not None and desk_mt != _last_desk_mtime:
                changed = True
            elif sig != _last_contracts_sig:
                # First observation seeds without writing.
                if _last_contracts_sig is None and _last_desk_mtime is None:
                    _last_desk_mtime = desk_mt
                    _last_contracts_sig = sig
                    return None
                changed = True
        if not changed:
            return None
        if not force and (now - _last_reproject_mono) < _MIN_REPROJECT_GAP_S:
            return None
        try:
            result = project_live(host="both", write=True)
        except Exception as e:
            if verbose:
                print(f"  [desk-project] failed ({reason or 'auto'}): {e}", flush=True)
            return {
                "ok": False,
                "error": "project_failed",
                "message": str(e),
                "reason": reason or "auto",
            }
        _last_reproject_mono = now
        _last_desk_mtime = desk_mt
        _last_contracts_sig = sig
        if verbose and result.get("ok"):
            print(
                f"  [desk-project] wrote apo-desk ({reason or 'auto'})",
                flush=True,
            )
        if isinstance(result, dict):
            result = dict(result)
            result["reason"] = reason or ("force" if force else "auto")
        return result
