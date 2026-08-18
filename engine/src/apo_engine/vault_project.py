"""Project desk policy markdown from ``vault(action=merge)`` IR.

Deterministic — no LLM. Returns shared ``body`` + optional ``guidance`` for placement.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

# Deterministic lines for usage-contract ``write_habits`` ids (projected into apo-desk).
_WRITE_HABIT_LINES: dict[str, str] = {
    "prefer_append_patch": (
        "- Prefer `append_note` / `patch_note` over full-file rewrites; archive via `patch_note` place op."
    ),
    "folder_on_search": (
        "- **Hard gate:** first `search_notes` in a turn **must** include `folder=` when PARA "
        "bucket is inferable (threads → `areas/threads`, config → `system/config`, etc.). "
        "Target ≥80% `folder_set/search_notes` in 7d rollups (`vault(action=stats)`)."
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
        "- **`vault` tool:** `vault(action=list|contracts|describe|merge|project|stats|lint)` — never "
        "`vault(name=…)` (`unexpected_keyword_argument:name`)."
    ),
    "end_of_turn_domain_writes": (
        "- **End-of-turn gate:** on consequential turns (decisions, status changes, new/corrected "
        "facts, completed tasks), write the **domain note** in the default/owning vault before the "
        "final reply — do not defer to end-of-session."
    ),
    "session_audit_telemetry_only": (
        "- Session audit is **automatic OTel** (Cursor hooks → Jaeger via otlp-mcp) — do **not** "
        "`append_note` to `vault=sessions` unless desk `dual_write.enabled` is explicitly true."
    ),
    "address_flaws_on_write": (
        "- After successful writes / `vault(action=lint)`, inspect **`flaws[]`** (corpus quality — "
        "not `tip`/`warning`). For `remediation: llm`, apply `suggested_op` once per finding per "
        "turn (`value: null` means supply content). For `human`, surface to operator. "
        "Stop if the same `code`+`path`+`evidence` reappears. Soft OKF may dual-emit prose "
        "`warnings` during the compat window — prefer branching on `flaws[].code`."
    ),
    "lint_before_conclude": (
        "- Before concluding a large vault hygiene pass, run `vault(action=lint, folder=…)` "
        "and drain `remediation: llm` findings within budget — do not unbounded lint→fix loops."
    ),
    "task_router_threads": (
        "- **Task routing:** tasks are threads with status action-needed. Create or update "
        "`areas/threads/<slug>.md` (`okf_type: Thread`, `status: active`, priority, "
        "`## Next action`) — do **not** create flat `tasks.md` entries."
    ),
    "filter_memory_type": (
        "- When a vault tags notes by `memory_type` instead of `okf_type`: stamp "
        "`memory_type` / `description` / `timestamp` on concept writes; prefer "
        "`filter_notes({\"memory_type\": \"…\"}, folder=…)`."
    ),
}

# Watch / multi-caller debounce for auto-reproject.
_reproject_lock = threading.Lock()
_last_reproject_mono = 0.0
_last_poll_mono = 0.0
_last_desk_mtime: float | None = None
_last_contracts_sig: str | None = None
_MIN_REPROJECT_GAP_S = 2.0
# Minimum gap between *drift scans*. `_contracts_signature()` reloads the vault
# registry (a usage-contract YAML parse per vault) and stats every contract file,
# which costs tens of ms on a real desk. Every vault watcher thread polls this
# once per wake, so an ungated scan burns N_vaults x cost every second — the
# watcher sat at ~35% CPU permanently idle. Detection may lag by this gap;
# projection is advisory/return-only, so that is free.
_MIN_POLL_GAP_S = float(os.environ.get("APO_DESK_POLL_GAP_S") or 15.0)


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


def _pointer_vault_id(raw: str) -> str | None:
    """Return vault_id from ``vault_id:rel`` pointer, or None if not vault-prefixed."""
    text = (raw or "").strip()
    if not text or ":" not in text:
        return None
    if text.startswith(("http://", "https://", "/")):
        return None
    vault_id, _, _ = text.partition(":")
    return vault_id.strip() or None


def scope_desk_overlay(desk: dict[str, Any], vaults: dict[str, Any]) -> dict[str, Any]:
    """Trim desk ``role_notes`` / ``pointers`` to vaults present in the active registry.

    Active vaults come from the merge ``vaults`` dict (APO_VAULTS / ``vaults=`` filter).
    Each binding carries a ``collection``; scoping is by vault name and assigned role.
    """
    out = dict(desk)
    active_names = set(vaults)
    active_roles = {
        str(row.get("role"))
        for row in vaults.values()
        if isinstance(row, dict) and row.get("role")
    }
    role_notes = desk.get("role_notes")
    if isinstance(role_notes, dict):
        out["role_notes"] = {
            role: note for role, note in role_notes.items() if role in active_roles
        }
    pointers = desk.get("pointers")
    if isinstance(pointers, dict):
        filtered: dict[str, Any] = {}
        for key, raw in pointers.items():
            vid = _pointer_vault_id(str(raw))
            if vid is None:
                # Absolute / URL / bare paths — keep (not cross-vault desk bleed).
                filtered[key] = raw
            elif vid in active_names:
                filtered[key] = raw
        out["pointers"] = filtered
    return out


def _md_link(label: str, path: str) -> str:
    if path.startswith("/"):
        return f"[{label}]({path})"
    return f"{label}: `{path}`"


def _contract_data(row: dict[str, Any], contract_id: str) -> dict[str, Any] | None:
    """Return a vault row's parsed ``<contract_id>`` body when present and ok.

    Requires ``vault_op("project", ...)`` to have merged with ``bodies=True`` —
    with ``bodies=False`` every contract entry is summary-only (``id``/``path``/
    ``source``/``ok``, no ``data``) and this always returns ``None``.
    """
    contracts = row.get("contracts") if isinstance(row.get("contracts"), dict) else {}
    entry = contracts.get(contract_id)
    if not isinstance(entry, dict) or not entry.get("ok", True):
        return None
    data = entry.get("data")
    return data if isinstance(data, dict) else None


def _usage_data(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return parsed usage-contract ``data`` when present and ok."""
    return _contract_data(row, "usage-contract")


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


def _usage_pointers(row: dict[str, Any]) -> list[str]:
    """Return usage-contract root-level ``pointers`` list when present.

    Distinct from ``contribution.pointers`` / ``integrations.pointers`` —
    this is the vault's own policy pointer list (memory policy, write API,
    session-log conventions, ...), otherwise silently dropped from every
    projected desk body even though the contract author put it there.
    """
    data = _usage_data(row)
    if not data:
        return []
    raw = data.get("pointers")
    if not isinstance(raw, list):
        return []
    return [p.strip() for p in raw if isinstance(p, str) and p.strip()]


def format_purpose_scope_lines(name: str, data: dict[str, Any]) -> list[str]:
    """Lines for apo-desk Vault purpose & scope section (deterministic)."""
    purpose = str(data.get("purpose") or "").strip()
    # First sentence only — full purpose can run to a paragraph; deep prose
    # belongs in a pointer, not the compiled skill.
    first_sentence = purpose.split(". ")[0].split("\n")[0].strip().rstrip(".")
    in_scope = _str_list(data.get("in_scope"))
    out_scope = _str_list(data.get("out_scope"))
    if not first_sentence and not in_scope and not out_scope:
        return []
    bits = [f"- `{name}`: " + (first_sentence + "." if first_sentence else "")]
    if in_scope:
        bits.append(f"  - in scope: {'; '.join(in_scope)}")
    if out_scope:
        bits.append(f"  - out of scope: {'; '.join(out_scope)}")
    return bits


def format_layout_line(name: str, layout: dict[str, Any]) -> str | None:
    """One-liner for apo-desk Folder layout section (deterministic)."""
    if not isinstance(layout, dict) or not layout:
        return None
    bits = [
        f"`{folder}`={str(rule).strip()}"
        for folder, rule in sorted(layout.items())
        if str(rule or "").strip()
    ]
    if not bits:
        return None
    return f"- `{name}`: " + "; ".join(bits)


def format_frontmatter_floor_line(name: str, keys: list[str]) -> str | None:
    """One-liner for apo-desk Frontmatter floor section (deterministic)."""
    if not keys:
        return None
    return f"- `{name}`: " + ", ".join(f"`{k}`" for k in keys)


def format_directive_lines(name: str, data: dict[str, Any]) -> list[str]:
    """Lines for apo-desk Vault directives section — usage-contract prose fields
    ``consult_vault_first`` / ``task_routing`` (deterministic, verbatim)."""
    lines: list[str] = []
    consult = str(data.get("consult_vault_first") or "").strip()
    if consult:
        lines.append(f"- `{name}` — consult first: {' '.join(consult.split())}")
    routing = str(data.get("task_routing") or "").strip()
    if routing:
        lines.append(f"- `{name}` — task routing: {' '.join(routing.split())}")
    return lines


def format_okf_path_rules_lines(
    name: str, okf: dict[str, Any], *, token_budget: int | None
) -> list[str]:
    """Table rows for apo-desk Type routing section from okf-contract ``path_rules``.

    Truncated to fit ``token_budget`` (rough chars/4 estimate) when set — the
    fullest contract in practice (atlas: ~30 rules) can otherwise dominate the
    compiled skill on its own.
    """
    rules = okf.get("path_rules")
    if not isinstance(rules, list) or not rules:
        return []
    rows: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        match = str(rule.get("match") or "").strip()
        if not match:
            continue
        enforcement = str(rule.get("enforcement") or "").strip()
        okf_type = str(rule.get("okf_type") or "").strip() or "—"
        required = _str_list(rule.get("required_fields"))
        req_s = ", ".join(f"`{f}`" for f in required) if required else "—"
        rows.append(f"| `{match}` | {enforcement or '—'} | {okf_type} | {req_s} |")
    if not rows:
        return []
    header = [f"### `{name}`", "", "| Match | Enforcement | Type | Required fields |", "|---|---|---|---|"]
    if token_budget:
        budget_chars = token_budget * 4
        kept: list[str] = []
        used = sum(len(line) for line in header)
        for row in rows:
            used += len(row) + 1
            if used > budget_chars and kept:
                remaining = len(rows) - len(kept)
                kept.append(f"| … | | | +{remaining} more — see okf-contract |")
                break
            kept.append(row)
        rows = kept
    return header + rows + [""]


def format_git_safety_lines(name: str, git: dict[str, Any]) -> list[str]:
    """Lines for apo-desk Git safety section from git-contract (deterministic)."""
    lines: list[str] = []
    never_commit = _str_list(git.get("never_commit"))
    sync = git.get("sync") if isinstance(git.get("sync"), dict) else {}
    on_block = str(sync.get("on_block_command") or "").strip()
    restore = git.get("restore") if isinstance(git.get("restore"), dict) else {}
    owner = str(restore.get("owner") or "").strip()
    drill = str(restore.get("drill") or "").strip()
    bits: list[str] = []
    if never_commit:
        bits.append("never_commit=" + ", ".join(f"`{g}`" for g in never_commit))
    if on_block:
        bits.append(f"on_block=`{on_block}`")
    if owner or drill:
        bits.append(f"restore={owner or '—'}" + (f" ({drill})" if drill else ""))
    if bits:
        lines.append(f"- `{name}`: " + "; ".join(bits))
    return lines


def format_telemetry_privacy_lines(name: str, tel: dict[str, Any]) -> list[str]:
    """Lines for apo-desk Telemetry privacy section from telemetry-contract."""
    privacy = tel.get("privacy") if isinstance(tel.get("privacy"), dict) else {}
    allow = privacy.get("allow") if isinstance(privacy.get("allow"), dict) else {}
    deny = _str_list(privacy.get("deny"))
    retention = tel.get("retention_days")
    access = tel.get("agent_access") if isinstance(tel.get("agent_access"), dict) else {}
    expose = _str_list(access.get("expose_paths"))
    bits: list[str] = []
    allow_dims = _str_list(allow.get("dimensions"))
    if allow_dims:
        bits.append("allow=" + ", ".join(f"`{d}`" for d in allow_dims))
    if deny:
        bits.append("deny=" + ", ".join(f"`{d}`" for d in deny))
    if retention:
        bits.append(f"retention={retention}d")
    if expose:
        bits.append("expose_paths=" + ", ".join(f"`{p}`" for p in expose))
    if not bits:
        return []
    return [f"- `{name}`: " + "; ".join(bits)]


def format_local_web_line(name: str, web: dict[str, Any]) -> str | None:
    """One-liner for apo-desk Local web browser section from local-web-contract."""
    bind = str(web.get("bind") or "").strip()
    port = web.get("port")
    mode = str(web.get("mode") or "").strip()
    if not bind and not port:
        return None
    addr = f"{bind or '—'}:{port}" if port else (bind or "—")
    bits = [f"`{addr}`"]
    if mode:
        bits.append(f"mode=`{mode}`")
    return f"- `{name}`: " + " ".join(bits)


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

def _usage_write_habits(row: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Return ``write_habits`` (id, inline_text) pairs from usage-contract ``data``.

    Each entry may be a bare string id (resolved against ``_WRITE_HABIT_LINES``)
    or an inline object ``{id: ..., text: ...}`` carrying vault-authored guidance
    directly — for a vault dialect that has no matching dict entry, this is how
    it gets real projected text instead of the generic fallback line.
    """
    data = _usage_data(row)
    if not data:
        return []
    raw = data.get("write_habits")
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, str | None]] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append((item.strip(), None))
        elif isinstance(item, dict):
            hid = str(item.get("id") or "").strip()
            if not hid:
                continue
            text = str(item.get("text") or "").strip() or None
            out.append((hid, text))
    return out


def _render_write_habit_lines(habits: list[tuple[str, str | None]]) -> list[str]:
    """Map usage-contract write_habit (id, inline_text) pairs to markdown bullets.

    Precedence: inline ``text`` from the contract, then a ``_WRITE_HABIT_LINES``
    dict entry, then a generic pointer-only fallback.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for hid, inline_text in habits:
        if hid in seen:
            continue
        seen.add(hid)
        if inline_text:
            lines.append(f"- **`{hid}`:** {inline_text}")
            continue
        line = _WRITE_HABIT_LINES.get(hid)
        if line:
            lines.append(line)
        else:
            lines.append(f"- `{hid}` — see usage-contract / apo-write-api.")
    return lines


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
    desk_meta = merge.get("desk_meta") if isinstance(merge.get("desk_meta"), dict) else {}
    if desk_meta.get("source") == "defaults":
        lines.append(
            "**No `~/.apo/desk.yaml` found** — the Role column below, dual-write, and "
            "workspace label are all inert built-in defaults, not real desk policy. "
            "Create `~/.apo/desk.yaml` to populate them."
        )
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
    scope_lines: list[str] = []
    layout_lines: list[str] = []
    frontmatter_lines: list[str] = []
    directive_lines: list[str] = []
    okf_lines: list[str] = []
    git_safety_lines: list[str] = []
    telemetry_lines: list[str] = []
    local_web_lines: list[str] = []
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
        contrib_pointer_raws.extend(_usage_pointers(row))

        usage = _usage_data(row)
        if usage:
            scope_lines.extend(format_purpose_scope_lines(name, usage))
            layout_line = format_layout_line(name, usage.get("layout") or {})
            if layout_line:
                layout_lines.append(layout_line)
            fm_line = format_frontmatter_floor_line(name, _str_list(usage.get("frontmatter_floor")))
            if fm_line:
                frontmatter_lines.append(fm_line)
            directive_lines.extend(format_directive_lines(name, usage))

        okf = _contract_data(row, "okf-contract")
        if okf:
            token_budget = usage.get("token_budget") if usage else None
            token_budget = token_budget if isinstance(token_budget, int) else None
            okf_lines.extend(format_okf_path_rules_lines(name, okf, token_budget=token_budget))

        git = _contract_data(row, "git-contract")
        if git:
            git_safety_lines.extend(format_git_safety_lines(name, git))

        telemetry = _contract_data(row, "telemetry-contract")
        if telemetry:
            telemetry_lines.extend(format_telemetry_privacy_lines(name, telemetry))

        local_web = _contract_data(row, "local-web-contract")
        if local_web:
            lw_line = format_local_web_line(name, local_web)
            if lw_line:
                local_web_lines.append(lw_line)
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

    if scope_lines:
        lines.append("## Vault purpose & scope")
        lines.append("")
        lines.extend(scope_lines)
        lines.append("")

    if layout_lines:
        lines.append("## Folder layout")
        lines.append("")
        lines.extend(layout_lines)
        lines.append("")

    if frontmatter_lines:
        lines.append("## Frontmatter floor")
        lines.append("")
        lines.append("Minimum frontmatter keys agents should stamp on concept notes, per vault.")
        lines.append("")
        lines.extend(frontmatter_lines)
        lines.append("")

    if directive_lines:
        lines.append("## Vault directives")
        lines.append("")
        lines.extend(directive_lines)
        lines.append("")

    if okf_lines:
        lines.append("## Type routing (OKF)")
        lines.append("")
        lines.append(
            "Folder → `okf_type` → required fields, from each vault's okf-contract "
            "`path_rules` (first match wins). Use this to pick `okf_type` and required "
            "fields when creating a concept note — do not guess."
        )
        lines.append("")
        lines.extend(okf_lines)

    if git_safety_lines:
        lines.append("## Git safety")
        lines.append("")
        lines.append("From each vault's git-contract — never-commit globs, sync block hook, restore drill.")
        lines.append("")
        lines.extend(git_safety_lines)
        lines.append("")

    if telemetry_lines:
        lines.append("## Telemetry privacy")
        lines.append("")
        lines.append("From each vault's telemetry-contract — what agent-facing telemetry captures and retains.")
        lines.append("")
        lines.extend(telemetry_lines)
        lines.append("")

    if local_web_lines:
        lines.append("## Local web browser")
        lines.append("")
        lines.extend(local_web_lines)
        lines.append("")

    sv = dual.get("session_vault") or "sessions"
    sp = dual.get("session_path_template") or "inbox/daily/{date}.md"
    sh = dual.get("session_heading") or "Session log"
    domain = dual.get("domain_vaults")
    if isinstance(domain, list) and domain:
        domain_s = ", ".join(f"`{d}`" for d in domain)
    else:
        # Default: writable domain vaults only — skip audit + grc (GRC SoT is git/PR).
        _skip_roles = {"audit", "grc", None}
        domain_s = ", ".join(
            f"`{n}`"
            for n, r in sorted(vaults.items())
            if isinstance(r, dict) and r.get("role") not in _skip_roles
            and n != sv
        ) or "`meta` / `norris` / `work` / `contracts`"
    dual_enabled = bool(dual.get("enabled"))
    if dual_enabled:
        lines.append("## Dual-write")
        lines.append("")
        lines.append(
            f"On consequential turns (and new durable facts when enabled): domain note in the owning vault "
            f"({domain_s}); **also** `append_note(..., vault=\"{sv}\")` on `{sp}` → `## {sh}` "
            f"with `YYYY-MM-DD HH:MM ET` (process turns: `Tooling:`). "
            f"Never write session logs to Meta/Norris/Work/Contracts dailies."
        )
    else:
        lines.append("## Session audit")
        lines.append("")
        lines.append(
            "Session working memory is **automatic OTel** (Cursor hooks → Jaeger via otlp-mcp). "
            "Do **not** `append_note` to `vault=sessions` on consequential turns."
        )
        lines.append("")
        lines.append(
            f"On consequential turns (decisions, status changes, new/corrected facts, completed tasks): "
            f"write the **domain note** in the owning/default vault ({domain_s}) before the final reply."
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
                "- **Write as you go, not at the end.** Capture new durable facts "
                "(dates, people, equipment, schedule, preferences, decisions) the moment "
                "they're learned. A turn that ends without a write it owed is a policy "
                "violation, not a wrap-up step skipped for later."
            )
        if habits.get("prefer_append_patch", True):
            lines.append("- Prefer `append_note` / `patch_note` over full-file rewrites; archive via `patch_note` place op.")
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
        throughput_ids = [h for h in throughput_ids if h[0] != "prefer_append_patch"]
    if habits.get("filter_okf_type", True):
        throughput_ids = [h for h in throughput_ids if h[0] != "filter_okf_type"]
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
    lines.append("- `delete_note` via `apo_admin(action=invoke, name=delete_note, confirm=true)` only.")
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


def project_live(vaults: list[str] | None = None) -> dict[str, Any]:
    """Build merge IR from the live registry/desk and project (CLI).

    ``vaults`` scopes to a named subset of the registry — see
    ``vault_op``'s docstring.
    """
    from . import ops

    return ops.vault_op("project", vaults=vaults)


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
    global _last_reproject_mono, _last_poll_mono, _last_desk_mtime, _last_contracts_sig

    with _reproject_lock:
        now = time.monotonic()
        # Drift scan is expensive; skip it entirely when polled again too soon.
        # Every vault watcher thread calls this once per wake, so this gate is
        # what keeps an idle desk off the CPU. `force` always scans.
        if not force and _last_poll_mono and (now - _last_poll_mono) < _MIN_POLL_GAP_S:
            return None
        _last_poll_mono = now
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
