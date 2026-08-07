"""Unified telemetry — agent MCP/RPC vs admin operator rollups."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from apo_engine import metrics_backend as mb
from apo_engine import telemetry_contract as tc
from apo_engine import tool_metrics as tm

AGENT_ACTIONS = frozenset({"status", "session", "active", "efficiency"})
ADMIN_ACTIONS = frozenset({"collection", "workbench", "events"})
ALL_ACTIONS = AGENT_ACTIONS | ADMIN_ACTIONS

Surface = Literal["agent", "admin"]

_DEFAULT_EFFICIENCY = {
    "folder_scoped_target_pct": 80,
    "expand_to_search_target_pct": 10,
    "error_rate_max_pct": 5,
    "large_read_bytes": 15000,
}


def _efficiency_thresholds(vault_root: Path | None) -> dict[str, Any]:
    data = tc.load_telemetry_contract(vault_root)
    if not data:
        return dict(_DEFAULT_EFFICIENCY)
    eff = data.get("efficiency")
    if not isinstance(eff, dict):
        return dict(_DEFAULT_EFFICIENCY)
    out = dict(_DEFAULT_EFFICIENCY)
    for key in _DEFAULT_EFFICIENCY:
        if key in eff and eff[key] is not None:
            out[key] = eff[key]
    return out


def _search_tool_slot(by_tool: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in by_tool:
        if row.get("tool") == "search_notes":
            return row
    return None


def compute_efficiency(
    events: list[dict[str, Any]],
    *,
    thresholds: dict[str, Any] | None = None,
    workbench: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Domain KPIs + tips from raw apo tool-call events."""
    th = thresholds or dict(_DEFAULT_EFFICIENCY)
    rollup = tm.rollup_events(events)
    by_tool = rollup.get("by_tool") or []
    search = _search_tool_slot(by_tool)
    search_calls = int(search.get("calls") or 0) if search else 0
    folder_set = int(search.get("folder_set") or 0) if search else 0
    folder_pct = round(100.0 * folder_set / search_calls, 1) if search_calls else 0.0
    target_pct = float(th.get("folder_scoped_target_pct") or 80)

    expand_calls = sum(
        int(r.get("calls") or 0)
        for r in by_tool
        if r.get("tool") in ("expand_section", "expand_chunk")
    )
    read_calls = sum(
        int(r.get("calls") or 0) for r in by_tool if r.get("tool") == "read_note"
    )
    expand_ratio = round(100.0 * expand_calls / search_calls, 1) if search_calls else 0.0

    total = len(events) or 1
    errors = int(rollup.get("error_count") or 0)
    error_rate = round(100.0 * errors / total, 1)

    patch_calls = sum(
        int(r.get("calls") or 0) for r in by_tool if r.get("tool") == "patch_note"
    )
    mtime_set = sum(
        int(r.get("expected_mtime_set") or 0)
        for r in by_tool
        if r.get("tool") == "patch_note"
    )

    top_shapes: list[dict[str, Any]] = []
    for shape, count in sorted(
        (rollup.get("by_error_shape") or {}).items(),
        key=lambda x: (-x[1], x[0]),
    )[:10]:
        tool_name = "?"
        for row in by_tool:
            shapes = row.get("by_error_shape") or {}
            if shape in shapes:
                tool_name = str(row.get("tool") or "?")
                break
        top_shapes.append({"tool": tool_name, "shape": shape, "count": count})

    tips: list[str] = []
    if search_calls and folder_pct < target_pct:
        tips.append(
            f"search_notes folder scoping {folder_pct}% below target {target_pct}%"
        )
    expand_target = float(th.get("expand_to_search_target_pct") or 10)
    if search_calls and expand_ratio < expand_target:
        tips.append(
            f"expand_section rarely used after search ({expand_ratio}% vs target {expand_target}%)"
        )
    max_err = float(th.get("error_rate_max_pct") or 5)
    if error_rate > max_err:
        tips.append(f"validation error rate {error_rate}% above max {max_err}%")
    if patch_calls and mtime_set < patch_calls * 0.5:
        tips.append("expected_mtime rarely set on patch_note repeat writes")

    orientation: dict[str, Any] = {}
    if workbench and workbench.get("ok"):
        orient = workbench.get("orientation_signal") or {}
        orientation = {
            "graphify_shell": orient.get("graphify_shell", 0),
            "grep_rg_shell": orient.get("grep_rg", orient.get("grep_shell", 0)),
            "graphify_log_queries": orient.get("graphify_log", 0),
            "subagent_spawns": (workbench.get("tool_mix") or {}).get("subagent", 0),
        }
        grep_n = int(orientation.get("grep_rg_shell") or 0)
        gf_n = int(orientation.get("graphify_shell") or 0)
        if grep_n > gf_n * 3 and grep_n > 5:
            tips.append("graphify underused vs rg for orientation")

    return {
        "ok": True,
        "calls": len(events),
        "thresholds": th,
        "search_notes": {
            "calls": search_calls,
            "folder_scoped_pct": folder_pct,
            "target_pct": target_pct,
            "avg_duration_ms": search.get("avg_duration_ms") if search else 0,
        },
        "read_patterns": {
            "read_note_calls": read_calls,
            "expand_section_calls": expand_calls,
            "expand_to_search_ratio_pct": expand_ratio,
            "target_ratio_pct": expand_target,
        },
        "validation": {
            "error_rate_pct": error_rate,
            "max_error_rate_pct": max_err,
            "top_error_shapes": top_shapes,
        },
        "write_habits": {
            "patch_note_calls": patch_calls,
            "expected_mtime_set": mtime_set,
        },
        "orientation": orientation,
        "tips": tips,
    }


def telemetry(
    action: str,
    *,
    surface: Surface = "agent",
    vault_root: Path | None = None,
    collection: str = "",
    days: int | None = 7,
    tool: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    act = (action or "status").strip().lower()
    allowed = ADMIN_ACTIONS if surface == "admin" else AGENT_ACTIONS
    if act not in allowed:
        other = "admin" if surface == "agent" else "agent"
        hint = (
            f"Use telemetry(action={act!r}) on the {other} surface "
            f"(MCP top-level: agent actions only; operator rollups: "
            f"apo_admin action=invoke name=telemetry)."
        )
        return {
            "ok": False,
            "error": "bad_action",
            "message": (
                f"action {act!r} not allowed for surface={surface!r}; "
                f"allowed: {', '.join(sorted(allowed))}"
            ),
            "hint": hint,
        }

    cfg = mb.resolve_store_config(vault_root)
    backend = mb.get_backend(vault_root)
    policy = tc.policy_for_vault(vault_root)

    if act == "status":
        store_status = backend.status()
        return {
            "ok": True,
            "action": "status",
            "enabled": mb.metrics_enabled(vault_root),
            "store": store_status,
            "engine_version": tm.engine_version(),
            "metrics_path": store_status.get("path") or cfg.path,
        }

    if act == "active":
        out = tm.read_active_session()
        out["action"] = "active"
        return out

    if act == "events":
        coll = (collection or "default").strip() or "default"
        events = backend.read_events(coll, days=days, tool=tool)
        return {
            "ok": True,
            "action": "events",
            "surface": surface,
            "collection": coll,
            "days": days,
            "tool_filter": tool or None,
            "count": len(events),
            "events": events,
        }

    if act == "workbench":
        if cfg.backend != "local":
            return {
                "ok": False,
                "error": "workbench_requires_local",
                "message": (
                    "workbench rollups require store.backend=local "
                    "(desk-metrics daemon)"
                ),
            }
        local = backend
        if not isinstance(local, mb.LocalDeskMetricsBackend):
            local = mb.LocalDeskMetricsBackend(cfg.local_uri)
        window = days if days is not None else 7
        report = local.workbench_report(window)
        report["action"] = "workbench"
        report["days"] = window
        report["surface"] = surface
        return report

    coll = (collection or "default").strip() or "default"

    if act == "collection":
        events = backend.read_events(coll, days=days, tool=tool)
        out = tm.rollup_events(events)
        out["action"] = "collection"
        out["surface"] = surface
        out["collection"] = coll
        out["days"] = days
        out["tool_filter"] = tool or None
        out["enabled"] = mb.metrics_enabled(vault_root)
        out["engine_version"] = tm.engine_version()
        st = backend.status()
        out["metrics_path"] = st.get("path") or st.get("uri") or str(cfg.path)
        return out

    if act == "session":
        cid = (conversation_id or "").strip() or None
        if not cid:
            from apo_engine.session_context import request_conversation_id

            cid = (request_conversation_id() or "").strip() or None
        if cid:
            events = backend.read_events(
                coll, days=days, tool=tool, conversation_id=cid
            )
            effective_days = days
        elif days is not None:
            events = backend.read_events(coll, days=days, tool=tool)
            effective_days = days
        else:
            events = backend.read_events(coll, days=1, tool=tool)
            effective_days = 1
        out = tm.rollup_events(events)
        out["action"] = "session"
        out["collection"] = coll
        out["days"] = effective_days
        out["tool_filter"] = tool or None
        out["conversation_id"] = cid
        out["enabled"] = mb.metrics_enabled(vault_root) and policy.enabled
        out["engine_version"] = tm.engine_version()
        st = backend.status()
        out["metrics_path"] = st.get("path") or st.get("uri") or str(cfg.path)
        if policy.expose_paths:
            out["by_path"] = tm.rollup_by_path(events)
        if not cid:
            out["tip"] = (
                "Session scope is approximate (24h window). "
                "Pass conversation_id for exact session attribution."
            )
        return out

    # efficiency
    window = days if days is not None else 7
    events = backend.read_events(coll, days=window, tool=tool)
    thresholds = _efficiency_thresholds(vault_root)
    workbench_data = None
    if cfg.backend == "local":
        try:
            lb = mb.LocalDeskMetricsBackend(cfg.local_uri)
            workbench_data = lb.workbench_report(window)
        except OSError:
            pass
    eff = compute_efficiency(
        events, thresholds=thresholds, workbench=workbench_data
    )
    eff["action"] = "efficiency"
    eff["collection"] = coll
    eff["days"] = window
    eff["engine_version"] = tm.engine_version()
    return eff
