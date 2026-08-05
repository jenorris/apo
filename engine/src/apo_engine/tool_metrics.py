"""MCP tool-use analytics — DuckDB under ~/.apo/metrics.duckdb.

Privacy dimensions are vault-contract-driven (``telemetry-contract.schema.yaml``).
Legacy ``tool-metrics-*.jsonl`` files are imported once on first open, then deleted.
"""
from __future__ import annotations

import json
import os
import re
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import duckdb

from apo_engine import telemetry_contract as tc

_JSONL_NAME = re.compile(r"^tool-metrics-(.+)\.jsonl$")
_write_lock = threading.Lock()
_FLAG_KEYS = (
    "folder_set",
    "fields_set",
    "expected_mtime_set",
    "used_alias",
    "ops_count",
    "error_shape",
)
_EXTRA_COLS = (
    ("vault_id", "VARCHAR"),
    ("conversation_id", "VARCHAR"),
    ("note_path", "VARCHAR"),
    ("path_hash", "VARCHAR"),
    ("heading", "VARCHAR"),
    ("chunk_hash", "VARCHAR"),
)


def _runtime_dir() -> Path:
    """Metrics directory — ~/.apo by default; APO_DEFERRED_DIR overrides (tests, sandboxes)."""
    raw = os.environ.get("APO_DEFERRED_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".apo"


DEFERRED_DIR = _runtime_dir()


def metrics_enabled() -> bool:
    raw = os.environ.get("APO_TOOL_METRICS")
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def metrics_db_path(path: Path | None = None) -> Path:
    """Shared DuckDB file for all collections."""
    return path if path is not None else DEFERRED_DIR / "metrics.duckdb"


def metrics_path(collection: str = "") -> Path:
    """Legacy alias — returns the shared metrics DuckDB path."""
    _ = collection
    return metrics_db_path()


def _estimate_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def extract_arg_flags(
    arguments: dict[str, Any] | None,
    *,
    tool: str | None = None,
) -> dict[str, Any]:
    """Privacy-safe arg fingerprints for rollups (no path/text/content)."""
    args = arguments if isinstance(arguments, dict) else {}
    flags: dict[str, Any] = {}
    if args.get("top_k") is not None:
        flags["used_alias"] = True
    if args.get("filters") is not None:
        flags["used_alias"] = True
    name = (tool or "").strip()
    if name == "append_note" and args.get("content") is not None:
        flags["used_alias"] = True
    if name == "write_note" and args.get("text") is not None:
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


def _connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


def _ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metrics_meta (
            key VARCHAR PRIMARY KEY,
            value VARCHAR
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tool_calls (
            ts VARCHAR NOT NULL,
            collection VARCHAR NOT NULL,
            tool VARCHAR NOT NULL,
            ok BOOLEAN NOT NULL,
            error VARCHAR,
            duration_ms DOUBLE,
            req_bytes INTEGER,
            resp_bytes INTEGER,
            folder_set BOOLEAN,
            fields_set BOOLEAN,
            expected_mtime_set BOOLEAN,
            used_alias BOOLEAN,
            ops_count INTEGER,
            error_shape JSON,
            vault_id VARCHAR,
            conversation_id VARCHAR,
            note_path VARCHAR,
            path_hash VARCHAR,
            heading VARCHAR,
            chunk_hash VARCHAR
        )
        """
    )
    for col, typ in _EXTRA_COLS:
        try:
            conn.execute(
                f"ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS {col} {typ}"
            )
        except duckdb.Error:
            pass
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tool_calls_collection_ts
        ON tool_calls (collection, ts)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tool_calls_conversation
        ON tool_calls (conversation_id, ts)
        """
    )


def _meta_get(conn: duckdb.DuckDBPyConnection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM metrics_meta WHERE key = ?", [key]
    ).fetchone()
    return str(row[0]) if row else None


def _meta_set(conn: duckdb.DuckDBPyConnection, key: str, value: str) -> None:
    conn.execute("DELETE FROM metrics_meta WHERE key = ?", [key])
    conn.execute(
        "INSERT INTO metrics_meta (key, value) VALUES (?, ?)", [key, value]
    )


def _collection_from_jsonl_path(path: Path) -> str:
    m = _JSONL_NAME.match(path.name)
    if m:
        coll = m.group(1).strip()
        return coll or "default"
    return "default"


def _parse_jsonl_file(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _error_shape_json(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def _insert_events(
    conn: duckdb.DuckDBPyConnection,
    collection: str,
    events: list[dict[str, Any]],
) -> None:
    if not events:
        return
    coll = (collection or "default").strip() or "default"
    for event in events:
        flags = {k: event.get(k) for k in _FLAG_KEYS if k in event}
        conn.execute(
            """
            INSERT INTO tool_calls (
                ts, collection, tool, ok, error, duration_ms, req_bytes, resp_bytes,
                folder_set, fields_set, expected_mtime_set, used_alias, ops_count, error_shape,
                vault_id, conversation_id, note_path, path_hash, heading, chunk_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event.get("ts"),
                coll,
                event.get("tool"),
                bool(event.get("ok")),
                event.get("error"),
                float(event.get("duration_ms") or 0),
                int(event.get("req_bytes") or 0),
                int(event.get("resp_bytes") or 0),
                bool(flags["folder_set"]) if flags.get("folder_set") else None,
                bool(flags["fields_set"]) if flags.get("fields_set") else None,
                bool(flags["expected_mtime_set"])
                if flags.get("expected_mtime_set")
                else None,
                bool(flags["used_alias"]) if flags.get("used_alias") else None,
                int(flags["ops_count"]) if flags.get("ops_count") is not None else None,
                _error_shape_json(flags.get("error_shape")),
                event.get("vault_id") or None,
                event.get("conversation_id") or None,
                event.get("note_path") or None,
                event.get("path_hash") or None,
                event.get("heading") or None,
                event.get("chunk_hash") or None,
            ],
        )


def _migrate_jsonl_once(db_path: Path) -> None:
    try:
        with _write_lock:
            conn = _connect(db_path)
            try:
                _ensure_schema(conn)
                if _meta_get(conn, "jsonl_migrated") == "1":
                    return
                jsonl_files = sorted(DEFERRED_DIR.glob("tool-metrics-*.jsonl"))
                if not jsonl_files:
                    _meta_set(conn, "jsonl_migrated", "1")
                    return
                for jpath in jsonl_files:
                    coll = _collection_from_jsonl_path(jpath)
                    rows = _parse_jsonl_file(jpath)
                    if rows:
                        _insert_events(conn, coll, rows)
                    jpath.unlink(missing_ok=False)
                _meta_set(conn, "jsonl_migrated", "1")
            finally:
                conn.close()
    except OSError:
        return
    except duckdb.Error:
        return


def _format_ts(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _row_to_event(row: tuple[Any, ...], columns: list[str]) -> dict[str, Any]:
    data = dict(zip(columns, row, strict=True))
    event: dict[str, Any] = {
        "ts": _format_ts(data.get("ts")),
        "tool": data.get("tool"),
        "ok": bool(data.get("ok")),
        "error": data.get("error"),
        "duration_ms": float(data.get("duration_ms") or 0),
        "req_bytes": int(data.get("req_bytes") or 0),
        "resp_bytes": int(data.get("resp_bytes") or 0),
    }
    for flag in ("folder_set", "fields_set", "expected_mtime_set", "used_alias"):
        if data.get(flag):
            event[flag] = True
    if data.get("ops_count") is not None:
        event["ops_count"] = int(data["ops_count"])
    raw_shape = data.get("error_shape")
    if raw_shape is not None:
        if isinstance(raw_shape, str):
            try:
                event["error_shape"] = json.loads(raw_shape)
            except json.JSONDecodeError:
                event["error_shape"] = raw_shape
        else:
            event["error_shape"] = raw_shape
    for key in ("vault_id", "conversation_id", "note_path", "path_hash", "heading", "chunk_hash"):
        if data.get(key):
            event[key] = data[key]
    return event


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
    vault_id: str = "",
    vault_root: Path | None = None,
    arguments: dict[str, Any] | None = None,
    conversation_id: str | None = None,
    path: Path | None = None,
) -> None:
    """Insert one tool-call event. Best-effort — never raises to callers."""
    if not metrics_enabled():
        return
    policy = tc.policy_for_vault(vault_root)
    if not policy.enabled:
        return
    db_path = metrics_db_path(path)
    event: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tool": tool,
        "ok": bool(ok),
        "error": error or None,
        "duration_ms": round(float(duration_ms), 2),
        "req_bytes": int(req_bytes),
        "resp_bytes": int(resp_bytes),
        "vault_id": (vault_id or policy.vault_id or "").strip() or None,
    }
    if flags:
        event.update(flags)
    if policy.record_conversation_id:
        from apo_engine.session_context import request_conversation_id

        cid = (conversation_id or request_conversation_id() or "").strip()
        if cid:
            event["conversation_id"] = cid
    event.update(tc.extract_note_context(tool, arguments, policy))
    try:
        _migrate_jsonl_once(db_path)
        with _write_lock:
            conn = _connect(db_path)
            try:
                _ensure_schema(conn)
                _insert_events(conn, collection, [event])
            finally:
                conn.close()
    except (OSError, duckdb.Error):
        return


def read_events(
    collection: str,
    *,
    days: int | None = None,
    tool: str | None = None,
    conversation_id: str | None = None,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    db_path = metrics_db_path(path)
    coll = (collection or "default").strip() or "default"
    tool_filter = (tool or "").strip() or None
    conv_filter = (conversation_id or "").strip() or None
    cutoff: datetime | None = None
    if days is not None and days > 0:
        cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
    try:
        _migrate_jsonl_once(db_path)
        if not db_path.is_file():
            return []
        with _write_lock:
            conn = _connect(db_path)
            try:
                _ensure_schema(conn)
                sql = """
                    SELECT ts, tool, ok, error, duration_ms, req_bytes, resp_bytes,
                           folder_set, fields_set, expected_mtime_set, used_alias,
                           ops_count, error_shape, vault_id, conversation_id,
                           note_path, path_hash, heading, chunk_hash
                    FROM tool_calls
                    WHERE collection = ?
                """
                params: list[Any] = [coll]
                if tool_filter:
                    sql += " AND tool = ?"
                    params.append(tool_filter)
                if conv_filter:
                    sql += " AND conversation_id = ?"
                    params.append(conv_filter)
                if cutoff is not None:
                    cutoff_ts = datetime.fromtimestamp(
                        cutoff, tz=timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
                    sql += " AND ts >= ?"
                    params.append(cutoff_ts)
                sql += " ORDER BY ts"
                cur = conn.execute(sql, params)
                columns = [d[0] for d in cur.description]
                rows = cur.fetchall()
            finally:
                conn.close()
    except (OSError, duckdb.Error):
        return []
    return [_row_to_event(row, columns) for row in rows]


def rollup_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_tool: dict[str, dict[str, Any]] = {}
    by_error: dict[str, int] = defaultdict(int)
    by_error_shape: dict[str, int] = defaultdict(int)
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
                "by_error_shape": defaultdict(int),
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
        shapes = ev.get("error_shape")
        if isinstance(shapes, list):
            for shape in shapes:
                if not shape:
                    continue
                key = str(shape)
                by_error_shape[key] += 1
                slot["by_error_shape"][key] += 1
        elif isinstance(shapes, str) and shapes:
            by_error_shape[shapes] += 1
            slot["by_error_shape"][shapes] += 1
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
        shape_counts = dict(
            sorted(slot["by_error_shape"].items(), key=lambda x: (-x[1], x[0]))
        )
        row = {
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
        if shape_counts:
            row["by_error_shape"] = shape_counts
        tools_out.append(row)

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
        "by_error_shape": dict(
            sorted(by_error_shape.items(), key=lambda x: (-x[1], x[0]))
        ),
        "by_day": dict(sorted(by_day.items())),
        "flags": dict(sorted(flag_counts.items())),
    }


def rollup_by_path(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    slots: dict[str, dict[str, Any]] = {}
    for ev in events:
        note_path = str(ev.get("note_path") or "").strip()
        if not note_path:
            continue
        slot = slots.setdefault(
            note_path,
            {"path": note_path, "calls": 0, "ok": 0, "error": 0, "tools": defaultdict(int)},
        )
        slot["calls"] += 1
        if ev.get("ok"):
            slot["ok"] += 1
        else:
            slot["error"] += 1
        slot["tools"][str(ev.get("tool") or "?")] += 1
    out = []
    for path, slot in sorted(slots.items(), key=lambda x: (-x[1]["calls"], x[0])):
        row = {
            "path": path,
            "calls": slot["calls"],
            "ok": slot["ok"],
            "error": slot["error"],
            "by_tool": dict(sorted(slot["tools"].items(), key=lambda x: (-x[1], x[0]))),
        }
        out.append(row)
    return out


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
    out["metrics_path"] = str(metrics_db_path(path))
    out["enabled"] = metrics_enabled()
    return out


def session_stats(
    collection: str,
    *,
    vault_root: Path | None = None,
    conversation_id: str | None = None,
    days: int | None = None,
    tool: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    policy = tc.policy_for_vault(vault_root)
    cid = (conversation_id or "").strip() or None
    if not cid:
        from apo_engine.session_context import request_conversation_id

        cid = (request_conversation_id() or "").strip() or None
    if cid:
        events = read_events(
            collection, days=days, tool=tool, conversation_id=cid, path=path
        )
    elif days is not None:
        events = read_events(collection, days=days, tool=tool, path=path)
    else:
        # Session scope without id: last 24h fallback
        events = read_events(collection, days=1, tool=tool, path=path)
    out = rollup_events(events)
    out["collection"] = collection
    out["days"] = days if cid else (days if days is not None else 1)
    out["tool_filter"] = tool or None
    out["conversation_id"] = cid
    out["metrics_path"] = str(metrics_db_path(path))
    out["enabled"] = metrics_enabled() and policy.enabled
    if policy.expose_paths:
        out["by_path"] = rollup_by_path(events)
    if not cid:
        out["tip"] = (
            "Session scope is approximate (24h window). "
            "Pass conversation_id, _apo.conversation_id, or _meta apo/conversation_id "
            "for exact session attribution."
        )
    return out


def read_active_session() -> dict[str, Any]:
    p = tc.active_session_path()
    if not p.is_file():
        return {"ok": True, "active": False, "path": str(p)}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "active": False, "path": str(p), "error": "unreadable"}
    if not isinstance(data, dict):
        return {"ok": False, "active": False, "path": str(p), "error": "invalid_json"}
    return {"ok": True, "active": True, "path": str(p), **data}


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
