#!/usr/bin/env bash
# Example Cursor hooks for Apo session telemetry (optional).
# Installed copy: ~/.cursor/hooks/apo-telemetry.sh — see docs/contracts/telemetry.md
set -euo pipefail

INPUT="$(cat)"

python3 - "$INPUT" <<'PY'
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def active_path() -> Path:
    raw = os.environ.get("APO_ACTIVE_SESSION_FILE", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".apo" / "active-session.json"


def conversation_id(payload: dict) -> str:
    for key in ("conversation_id", "session_id"):
        val = str(payload.get(key) or "").strip()
        if val:
            return val
    return ""


def write_active(payload: dict, *, refresh_only: bool = False) -> None:
    cid = conversation_id(payload)
    path = active_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if refresh_only and path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                existing = data
        except (OSError, json.JSONDecodeError):
            existing = {}
    if not cid and refresh_only:
        cid = str(existing.get("conversation_id") or existing.get("session_id") or "").strip()
    if not cid and not refresh_only:
        return
    out = {
        "conversation_id": cid or existing.get("conversation_id") or existing.get("session_id"),
        "started_at": existing.get("started_at")
        or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "workspace_roots": payload.get("workspace_roots")
        or existing.get("workspace_roots")
        or [],
        "composer_mode": payload.get("composer_mode") or existing.get("composer_mode"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if payload.get("is_background_agent") is not None:
        out["is_background_agent"] = bool(payload.get("is_background_agent"))
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")


def is_apo_server(payload: dict) -> bool:
    server = str(payload.get("server") or payload.get("server_name") or "").strip().lower()
    if server and "apo" in server:
        return True
    cmd = str(payload.get("command") or "").lower()
    return "apo" in cmd and "server.py" in cmd


raw = sys.argv[1] if len(sys.argv) > 1 else ""
if not raw.strip():
    sys.exit(0)

try:
    payload = json.loads(raw)
except json.JSONDecodeError:
    sys.exit(0)

if not isinstance(payload, dict):
    sys.exit(0)

event = str(payload.get("hook_event_name") or "").strip()

if event == "sessionStart":
    write_active(payload)
    cid = conversation_id(payload)
    if cid:
        print(json.dumps({"env": {"APO_CONVERSATION_ID": cid}}))
    sys.exit(0)

if event == "beforeMCPExecution":
    if is_apo_server(payload):
        write_active(payload, refresh_only=True)
    print(json.dumps({"permission": "allow"}))
    sys.exit(0)

sys.exit(0)
PY
