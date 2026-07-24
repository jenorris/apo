"""MCP tool-use analytics — append-only JSONL under ~/.apo/.

Records call metadata only (no note bodies or paths). Used to tune the MCP
surface from real desk metrics. Disable with APO_TOOL_METRICS=0/false/no/off.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFERRED_DIR = Path.home() / ".apo"


def metrics_enabled() -> bool:
    raw = os.environ.get("APO_TOOL_METRICS")
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def metrics_path(collection: str) -> Path:
    coll = (collection or "default").strip() or "default"
    return DEFERRED_DIR / f"tool-metrics-{coll}.jsonl"


def _estimate_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def extract_arg_flags(arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Privacy-safe arg fingerprints for rollups (no path/text/content)."""
    args = arguments if isinstance(arguments, dict) else {}
    flags: dict[str, Any] = {}
    if args.get("top_k") is not None:
        flags["used_alias"] = True
    if args.get("filters") is not None:
        flags["used_alias"] = True
    if args.get("folder"):
        flags["folder_set"] = True
    if args.get("fields") is not None:
        flags["fields_set"] = True
    if args.get("expected_mtime") is not None:
        flags["expected_mtime_set"] = True
    ops = args.get("ops")
    if isinstance(ops, list):
        flags["ops_count"] = len(ops)
    return flags


def record_call(
    *,
    collection: str,
    tool: str,
    ok: bool,
    error: str | None = None,
    duration_ms: float = 0.0,
    req_bytes: int = 0,
    resp_bytes: int = 0,
    flags: dict[str, Any] | None = None,
    path: Path | None = None,
) -> None:
    """Append one JSONL event. Best-effort — never raises to callers."""
    if not metrics_enabled():
        return
    event = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tool": tool,
        "ok": bool(ok),
        "error": error or None,
        "duration_ms": round(float(duration_ms), 2),
        "req_bytes": int(req_bytes),
        "resp_bytes": int(resp_bytes),
    }
    if flags:
        event.update(flags)
    dest = path or metrics_path(collection)
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(line)
            f.flush()
    except OSError:
        return


def _parse_ts(ts: str) -> datetime | None:
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def read_events(
    collection: str,
    *,
    days: int | None = None,
    tool: str | None = None,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    dest = path or metrics_path(collection)
    if not dest.is_file():
        return []
    cutoff: datetime | None = None
    if days is not None and days > 0:
        cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
    tool_filter = (tool or "").strip() or None
    out: list[dict[str, Any]] = []
    try:
        with open(dest, "r", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                if tool_filter and row.get("tool") != tool_filter:
                    continue
                if cutoff is not None:
                    parsed = _parse_ts(str(row.get("ts") or ""))
                    if parsed is None:
                        continue
                    if parsed.timestamp() < cutoff:
                        continue
                out.append(row)
    except OSError:
        return []
    return out


def rollup_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_tool: dict[str, dict[str, Any]] = {}
    by_error: dict[str, int] = defaultdict(int)
    by_day: dict[str, int] = defaultdict(int)
    flag_counts: dict[str, int] = defaultdict(int)
    total_ok = 0
    total_err = 0
    dur_sum = 0.0
    req_sum = 0
    resp_sum = 0

    for ev in events:
        name = str(ev.get("tool") or "?")
        slot = by_tool.setdefault(
            name,
            {
                "calls": 0,
                "ok": 0,
                "error": 0,
                "duration_ms_sum": 0.0,
                "req_bytes_sum": 0,
                "resp_bytes_sum": 0,
                "expected_mtime_set": 0,
                "fields_set": 0,
                "folder_set": 0,
                "used_alias": 0,
            },
        )
        slot["calls"] += 1
        ok = bool(ev.get("ok"))
        if ok:
            slot["ok"] += 1
            total_ok += 1
        else:
            slot["error"] += 1
            total_err += 1
            err = str(ev.get("error") or "error")
            by_error[err] += 1
        dur = float(ev.get("duration_ms") or 0)
        slot["duration_ms_sum"] += dur
        dur_sum += dur
        req = int(ev.get("req_bytes") or 0)
        resp = int(ev.get("resp_bytes") or 0)
        slot["req_bytes_sum"] += req
        slot["resp_bytes_sum"] += resp
        req_sum += req
        resp_sum += resp
        for flag in (
            "expected_mtime_set",
            "fields_set",
            "folder_set",
            "used_alias",
        ):
            if ev.get(flag):
                slot[flag] += 1
                flag_counts[flag] += 1
        ts = str(ev.get("ts") or "")
        day = ts[:10] if len(ts) >= 10 else "?"
        by_day[day] += 1

    tools_out = []
    for name, slot in sorted(by_tool.items(), key=lambda x: (-x[1]["calls"], x[0])):
        calls = slot["calls"] or 1
        tools_out.append(
            {
                "tool": name,
                "calls": slot["calls"],
                "ok": slot["ok"],
                "error": slot["error"],
                "avg_duration_ms": round(slot["duration_ms_sum"] / calls, 2),
                "avg_req_bytes": round(slot["req_bytes_sum"] / calls),
                "avg_resp_bytes": round(slot["resp_bytes_sum"] / calls),
                "expected_mtime_set": slot["expected_mtime_set"],
                "fields_set": slot["fields_set"],
                "folder_set": slot["folder_set"],
                "used_alias": slot["used_alias"],
            }
        )

    n = len(events) or 1
    return {
        "ok": True,
        "calls": len(events),
        "ok_count": total_ok,
        "error_count": total_err,
        "avg_duration_ms": round(dur_sum / n, 2) if events else 0.0,
        "avg_req_bytes": round(req_sum / n) if events else 0,
        "avg_resp_bytes": round(resp_sum / n) if events else 0,
        "by_tool": tools_out,
        "by_error": dict(sorted(by_error.items(), key=lambda x: (-x[1], x[0]))),
        "by_day": dict(sorted(by_day.items())),
        "flags": dict(sorted(flag_counts.items())),
    }


def tool_stats(
    collection: str,
    *,
    days: int | None = 7,
    tool: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    events = read_events(collection, days=days, tool=tool, path=path)
    out = rollup_events(events)
    out["collection"] = collection
    out["days"] = days
    out["tool_filter"] = tool or None
    out["metrics_path"] = str(path or metrics_path(collection))
    out["enabled"] = metrics_enabled()
    return out


def summarize_result(result: Any) -> tuple[bool, str | None, int]:
    """Infer ok/error and response size from a tool return value."""
    resp_bytes = _estimate_bytes(result)
    if isinstance(result, dict):
        ok = bool(result.get("ok", True))
        err = None if ok else str(result.get("error") or "error")
        return ok, err, resp_bytes
    return True, None, resp_bytes


# Re-export helpers used by middleware
estimate_bytes = _estimate_bytes
