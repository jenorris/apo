"""Apo habit KPI rollups — internal engine API for vault(action=stats)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apo_engine import metrics_backend as mb
from apo_engine import telemetry_contract as tc
from apo_engine import tool_metrics as tm

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


def _read_chunk_calls(events: list[dict[str, Any]]) -> int:
    count = 0
    for ev in events:
        if ev.get("tool") != "read_note":
            continue
        if ev.get("chunk_hash"):
            count += 1
            continue
        args = ev.get("args") if isinstance(ev.get("args"), dict) else {}
        if args.get("chunk_hash"):
            count += 1
            continue
        flags = ev.get("flags") if isinstance(ev.get("flags"), dict) else {}
        if flags.get("chunk_hash_set") or ev.get("chunk_hash_set"):
            count += 1
    return count


def compute_efficiency(
    events: list[dict[str, Any]],
    *,
    thresholds: dict[str, Any] | None = None,
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

    legacy_expand = sum(
        int(r.get("calls") or 0)
        for r in by_tool
        if r.get("tool") in ("expand_section", "expand_chunk")
    )
    chunk_reads = _read_chunk_calls(events) + legacy_expand
    expand_ratio = round(100.0 * chunk_reads / search_calls, 1) if search_calls else 0.0

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
            f"read_note(chunk_hash=) rarely used after search ({expand_ratio}% vs target {expand_target}%)"
        )
    max_err = float(th.get("error_rate_max_pct") or 5)
    if error_rate > max_err:
        tips.append(f"validation error rate {error_rate}% above max {max_err}%")
    if patch_calls and mtime_set < patch_calls * 0.5:
        tips.append("expected_mtime rarely set on patch_note repeat writes")

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
            "read_note_chunk_calls": chunk_reads,
            "chunk_read_to_search_ratio_pct": expand_ratio,
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
        "flaws": {
            "emitted": sum(int(ev.get("flaws_emitted") or 0) for ev in events),
            "auto_fixed": sum(int(ev.get("flaws_auto_fixed") or 0) for ev in events),
        },
        "tips": tips,
    }


def vault_stats(
    *,
    vault_root: Path | None = None,
    collection: str = "",
    days: int | None = 7,
) -> dict[str, Any]:
    """Habit KPI rollup for vault(action=stats)."""
    coll = (collection or "default").strip() or "default"
    window = days if days is not None else 7
    backend = mb.get_backend(vault_root)
    events = backend.read_events(coll, days=window)
    thresholds = _efficiency_thresholds(vault_root)
    eff = compute_efficiency(events, thresholds=thresholds)
    eff["action"] = "stats"
    eff["collection"] = coll
    eff["days"] = window
    eff["engine_version"] = tm.engine_version()
    eff["enabled"] = mb.metrics_enabled(vault_root)
    st = backend.status()
    eff["metrics_path"] = st.get("path") or str(mb.resolve_store_config(vault_root).path)
    eff["store"] = {"backend": st.get("backend") or "embedded", "path": eff["metrics_path"]}
    return eff


def telemetry(
    action: str,
    *,
    surface: str = "agent",
    vault_root: Path | None = None,
    collection: str = "",
    days: int | None = 7,
    tool: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Deprecated RPC shim — use vault_stats for efficiency habits."""
    del surface, tool, conversation_id
    act = (action or "efficiency").strip().lower()
    if act in ("efficiency", "stats"):
        return vault_stats(vault_root=vault_root, collection=collection, days=days)
    return {
        "ok": False,
        "error": "bad_action",
        "message": (
            f"action {act!r} removed in v0.5.0 — use vault(action=stats) for habits; "
            "operator traces via otlp-mcp + Jaeger"
        ),
    }
