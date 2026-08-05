#!/usr/bin/env bash
# Apo session telemetry — copy to ~/.cursor/hooks/apo-telemetry.sh
# Wire docs: docs/contracts/telemetry.md
set -euo pipefail

INPUT="$(cat)"

python3 - "$INPUT" <<'PY'
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

APO_MCP_TOOLS = {
    "search_notes", "expand_chunk", "read_note", "write_note", "append_note",
    "patch_note", "place_note", "filter_notes", "backlinks", "history", "vault",
    "session_stats", "active_session", "reload_config", "memory_status",
    "reindex_deferred", "reindex", "delete_note", "tool_stats", "git_sync",
}


def active_path() -> Path:
    raw = os.environ.get("APO_ACTIVE_SESSION_FILE", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".apo" / "active-session.json"


def conversation_id(payload: dict) -> str:
    for key in ("conversation_id", "session_id"):
        val = str(payload.get(key) or "").strip()
        if val:
            return val
    return ""


def generation_id(payload: dict) -> str:
    return str(payload.get("generation_id") or "").strip()


def write_active(payload: dict) -> None:
    cid = conversation_id(payload)
    if not cid:
        return
    path = active_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "conversation_id": cid,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "workspace_roots": payload.get("workspace_roots") or [],
        "composer_mode": payload.get("composer_mode"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if payload.get("is_background_agent") is not None:
        out["is_background_agent"] = bool(payload.get("is_background_agent"))
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")


def is_apo_tool(tool_name: str) -> bool:
    name = tool_name.strip()
    if not name:
        return False
    if name in APO_MCP_TOOLS:
        return True
    lower = name.lower()
    return "apo" in lower and ("mcp" in lower or ":" in name)


def parse_tool_input(raw: object) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


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
cid = conversation_id(payload)
gid = generation_id(payload)

if event == "sessionStart":
    write_active(payload)
    if cid:
        print(json.dumps({"env": {"APO_CONVERSATION_ID": cid}}))
    sys.exit(0)

if event == "preToolUse":
    tool_name = str(payload.get("tool_name") or "")
    if not cid or not is_apo_tool(tool_name):
        print(json.dumps({"permission": "allow"}))
        sys.exit(0)
    tool_input = parse_tool_input(payload.get("tool_input"))
    apo = tool_input.get("_apo")
    if not isinstance(apo, dict):
        apo = {}
    apo = dict(apo)
    apo.setdefault("conversation_id", cid)
    if gid:
        apo.setdefault("generation_id", gid)
    tool_input["_apo"] = apo
    print(json.dumps({"permission": "allow", "updated_input": tool_input}))
    sys.exit(0)

if event == "beforeMCPExecution":
    print(json.dumps({"permission": "allow"}))
    sys.exit(0)

sys.exit(0)
PY
