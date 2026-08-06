"""Project desk policy markdown from ``vault(action=merge)`` IR.

Deterministic — no LLM. Returns shared ``body`` + optional ``guidance`` for placement.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from . import vault_contracts

# Deterministic lines for usage-contract ``write_habits`` ids (projected into apo-desk).
_WRITE_HABIT_LINES: dict[str, str] = {
    "prefer_append_patch": (
        "- Prefer `append_note` / `patch_note` over full-file rewrites; archive via `place_note`."
    ),
    "folder_on_search": (
        "- **Hard gate:** first `search_notes` in a turn **must** include `folder=` when PARA "
        "bucket is inferable (threads → `areas/threads`, config → `system/config`, etc.). "
        "Target ≥80% `folder_set/search_notes` in 7d rollups (`just tool-stats`)."
    ),
    "expected_mtime_on_followup": (
        "- On **second+** write to the same path in one session, pass `expected_mtime` from the "
        "prior hit's float `mtime`; on `stale_write`, re-read and reapply."
    ),
    "filter_okf_type": (
        "- When a vault has an OKF contract: stamp `okf_type` / `description` / `timestamp` on "
        "concept writes; prefer `filter_notes({\"okf_type\": \"…\"}, folder=…)`."
    ),
    "patch_note_wire": (
        "- **`patch_note` wire:** every op needs `\"op\"`; `set_field` uses `field=` not `path`; "
        "`replace_text` uses `find`/`replace` not `old_text`/`new_text`; section ops use `text=` "
        "not `content=`; batch uses `items=[{path, ops}]`."
    ),
    "chunk_hash_surgical": (
        "- Search → write via `chunk_hash` / `heading` from hits — skip `read_note` when an "
        "anchor exists (`append_note(chunk_hash=…)` or patch with `chunk_hash`)."
    ),
    "dedupe_search": (
        "- **One** `search_notes` per distinct query per turn — no parallel duplicate searches; "
        "batch facets with `folder=` or widen `limit=` instead."
    ),
    "vault_api_routing": (
        "- **`vault` tool:** `vault(action=list|contracts|describe|merge|project)` — never "
        "`vault(name=…)` (`unexpected_keyword_argument:name`)."
    ),
}

# Watch / multi-caller debounce for auto-reproject.
_reproject_lock = threading.Lock()
_last_reproject_mono = 0.0
_last_desk_mtime: float | None = None
_last_contracts_sig: str | None = None
_MIN_REPROJECT_GAP_S = 2.0


def project_guidance() -> str:
    """Non-prescriptive placement hint — agent chooses surface and frontmatter."""
    return (
        "Return-only desk policy. Place `body` in whichever instruction surface your "
        "agent host already uses (rule, skill, AGENTS section, etc.). Apo does not "
        "prescribe paths or frontmatter. Re-run `vault(action=project)` after "
        "`~/.apo/desk.yaml` or vault `system/contracts/` changes."
    )


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


def _usage_data(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return parsed usage-contract ``data`` when present and ok."""
    contracts = row.get("contracts") if isinstance(row.get("contracts"), dict) else {}
    entry = contracts.get("usage-contract")
    if not isinstance(entry, dict) or not entry.get("ok", True):
        return None
    data = entry.get("data")
    return data if isinstance(data, dict) else None


def _usage_contribution(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return usage-contract ``contribution`` mapping when present."""
    data = _usage_data(row)
    if not data:
        return None
    contrib = data.get("contribution")
    return contrib if isinstance(contrib, dict) else None


def _usage_integrations(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return usage-contract ``integrations`` mapping when present."""
    data = _usage_data(row)
    if not data:
        return None
    integ = data.get("integrations")
    return integ if isinstance(integ, dict) else None


def _str_list(raw: Any) -> list[str]:
    """Normalize host keys / CLI names (YAML list or single scalar string)."""
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _fmt_host_keys(keys: list[str]) -> str:
    return ", ".join(f"`{k}`" for k in keys) if keys else "—"


def format_integrations_line(name: str, integ: dict[str, Any]) -> str:
    """One-liner for apo-desk Expected integrations section (deterministic)."""
    mcp = integ.get("mcp") if isinstance(integ.get("mcp"), dict) else {}
    required = _str_list(mcp.get("required"))
    expected = _str_list(mcp.get("expected"))
    optional = _str_list(mcp.get("optional"))
    never = _str_list(mcp.get("never"))
    cli_raw = integ.get("cli")
    if isinstance(cli_raw, dict):
        cli = _str_list(cli_raw.get("expected"))
    else:
        cli = _str_list(cli_raw)

    bits = [
        f"required={_fmt_host_keys(required)}",
        f"expected={_fmt_host_keys(expected)}",
        f"optional={_fmt_host_keys(optional)}",
        f"never={_fmt_host_keys(never)}",
    ]
    if cli:
        bits.append(f"cli={_fmt_host_keys(cli)}")
    return f"- `{name}`: " + "; ".join(bits)


def format_mcp_proxy_lines(name: str, integ: dict[str, Any]) -> list[str]:
    """Lines for apo-desk MCP proxy section when ``integrations.mcp.proxy`` is set."""
    mcp = integ.get("mcp") if isinstance(integ.get("mcp"), dict) else {}
    proxy = mcp.get("proxy")
    if not isinstance(proxy, dict):
        return []
    proxy_name = str(proxy.get("name") or "").strip()
    if not proxy_name:
        return []

    via = _str_list(proxy.get("via"))
    parked = _str_list(proxy.get("parked"))
    catalog = str(proxy.get("catalog") or "").strip()
    enable = str(proxy.get("enable_parked") or "").strip()
    invoke = str(
        proxy.get("invoke")
        or "list_servers → list_commands → invoke_command "
        "(nest downstream args under `parameters` only)"
    ).strip()

    lines = [
        f"- `{name}`: use Cursor MCP host `{proxy_name}` for proxied stdio servers "
        f"(do not expect them as top-level Cursor tools)."
    ]
    if via:
        lines.append(f"  - via `{proxy_name}`: {_fmt_host_keys(via)}")
    if parked:
        lines.append(f"  - parked (off until enabled): {_fmt_host_keys(parked)}")
    if catalog:
        lines.append(f"  - catalog: `{catalog}`")
    if enable:
        lines.append(f"  - enable parked: {enable}")
    lines.append(f"  - call pattern: {invoke}")
    return lines


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
            parts: list[str] = []
            if srow.get("dialect"):
                parts.append(str(srow["dialect"]))
            if (
                srow.get("callouts") is not None
                and srow["callouts"] != features.get("callouts")
            ):
                parts.append(f"callouts {srow['callouts']}")
            if parts:
                extras.append(f"{sname}=" + "/".join(parts))

    render = contrib.get("render")
    if isinstance(render, dict):
        profile = str(render.get("profile") or "").strip()
        if profile:
            extras.append(f"render `{profile}`")

    if extras:
        return f"- `{name}`: `{dialect}` ({'; '.join(extras)})"
    return f"- `{name}`: `{dialect}`"

def _usage_write_habits(row: dict[str, Any]) -> list[str]:
    """Return ``write_habits`` id list from usage-contract ``data``."""
    data = _usage_data(row)
    if not data:
        return []
    raw = data.get("write_habits")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _render_write_habit_lines(habit_ids: list[str]) -> list[str]:
    """Map usage-contract write_habit ids to projected markdown bullets."""
    lines: list[str] = []
    seen: set[str] = set()
    for hid in habit_ids:
        if hid in seen:
            continue
        seen.add(hid)
        line = _WRITE_HABIT_LINES.get(hid)
        if line:
            lines.append(line)
        else:
            lines.append(f"- `{hid}` — see usage-contract / apo-write-api.")
    return lines


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
        "+ per-vault `system/contracts/`. **Return-only** — agent places `body`; "
        "re-run `just desk-project` or `vault(action=project)` after desk/contract changes."
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
    integ_lines: list[str] = []
    proxy_lines: list[str] = []
    contrib_pointer_raws: list[str] = []
    for name, row in sorted(vaults.items()):
        if not isinstance(row, dict):
            continue
        contrib = _usage_contribution(row)
        if contrib:
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
        integ = _usage_integrations(row)
        if integ:
            integ_lines.append(format_integrations_line(name, integ))
            proxy_lines.extend(format_mcp_proxy_lines(name, integ))
            iptrs = integ.get("pointers")
            if isinstance(iptrs, list):
                for p in iptrs:
                    if isinstance(p, str) and p.strip():
                        contrib_pointer_raws.append(p.strip())
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

    if integ_lines:
        lines.append("## Expected integrations")
        lines.append("")
        lines.append(
            "Per-vault MCP host keys / CLI names from usage-contract `integrations` "
            "(advisory — does not start Cursor MCP processes). "
            "`required` / `expected` / `optional` / `never` guide tool choice by vault domain."
        )
        lines.append("")
        lines.extend(integ_lines)
        lines.append("")

    if proxy_lines:
        lines.append("## MCP proxy")
        lines.append("")
        lines.append(
            "When `integrations.mcp.proxy` is set, listed `via` / `parked` servers are "
            "**not** top-level Cursor MCP tools — call them through the named proxy "
            "(typically `lazy-mcp`). Parked servers stay off until enabled in the catalog."
        )
        lines.append("")
        lines.extend(proxy_lines)
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

    # Apo throughput — from default vault usage-contract write_habits (deterministic projection).
    default_vault = default or next(iter(vaults.keys()), "")
    default_row = vaults.get(default_vault) if isinstance(vaults.get(default_vault), dict) else {}
    throughput_ids = _usage_write_habits(default_row if isinstance(default_row, dict) else {})
    # Skip ids already rendered by desk.yaml boolean habits (avoid duplicate bullets).
    if habits.get("prefer_append_patch", True):
        throughput_ids = [h for h in throughput_ids if h != "prefer_append_patch"]
    if habits.get("filter_okf_type", True):
        throughput_ids = [h for h in throughput_ids if h != "filter_okf_type"]
    throughput_lines = _render_write_habit_lines(throughput_ids)
    if throughput_lines:
        lines.append("## Apo throughput")
        lines.append("")
        lines.append(
            "From default vault usage-contract `write_habits` — engine API detail in skill **`mcp-apo`** "
            "and Meta `system/config/apo-write-api.md`."
        )
        lines.append("")
        lines.extend(throughput_lines)
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


def project(merge: dict[str, Any]) -> dict[str, Any]:
    """Render desk policy from merge IR (return-only — agent places)."""
    body = render_desk_body(merge)
    return {
        "ok": True,
        "action": "project",
        "body": body,
        "bytes": len(body.encode("utf-8")),
        "guidance": project_guidance(),
    }


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


def project_live() -> dict[str, Any]:
    """Build merge IR from the live registry/desk and project (CLI)."""
    from . import ops

    return ops.vault_op("project")


def maybe_reproject(
    *,
    reason: str = "",
    force: bool = False,
    verbose: bool = False,
) -> dict[str, Any] | None:
    """Track desk.yaml / ``system/contracts/`` changes (projection is return-only).

    Debounced across vault watcher threads. Returns a change marker when desk or
    contracts drift; does not write host skill/rule files.
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
                if _last_contracts_sig is None and _last_desk_mtime is None:
                    _last_desk_mtime = desk_mt
                    _last_contracts_sig = sig
                    return None
                changed = True
        if not changed:
            return None
        if not force and (now - _last_reproject_mono) < _MIN_REPROJECT_GAP_S:
            return None
        _last_reproject_mono = now
        _last_desk_mtime = desk_mt
        _last_contracts_sig = sig
        if verbose:
            print(
                f"  [desk-project] desk/contracts changed ({reason or 'auto'}) — "
                "run desk-project to render",
                flush=True,
            )
        return {
            "ok": True,
            "changed": True,
            "reason": reason or ("force" if force else "auto"),
            "tip": "Run `just desk-project` or vault(action=project) and place the rendered text",
        }
