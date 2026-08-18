"""Optima Stage B — contract-driven merge of domain schedules → ``current.yaml``.

Ports vault ``system/scripts/optima_merge.py`` into the engine. Desk owns ingest;
this module only reads vault paths and writes Optima outputs. No ``gws``/GCal.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from . import optima_contract, vaults
from .optima_contract import MergeSettings, SourceSpec

TZ = ZoneInfo("America/New_York")

KIND_PRIORITY = [
    "incident",
    "meeting",
    "personal",
    "habit_projection",
    "focus",
    "work_private",
    "default_work",
    "free",
]


def load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_md_frontmatter(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        # Pure YAML catalog (Meta schedule / Optima override style)
        return load_yaml_file(path)
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        data = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def reachability_for(kind: str, rules: dict[str, Any]) -> dict[str, Any]:
    by_kind = rules.get("by_kind") or {}
    row = by_kind.get(kind) or by_kind.get("free") or {
        "slack": "ok",
        "email": "ok",
        "telegram": "ok",
        "phone": "ok",
        "rationale": "default",
    }
    return {
        "slack": row.get("slack", "ok"),
        "email": row.get("email", "ok"),
        "telegram": row.get("telegram", "ok"),
        "phone": row.get("phone", "ok"),
        "rationale": row.get("rationale") or kind,
    }


def _ended(block: dict[str, Any], now: datetime) -> bool:
    end = block.get("end")
    if not end:
        return False
    try:
        edt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return False
    if edt.tzinfo is None:
        edt = edt.replace(tzinfo=TZ)
    return edt <= now


def pick_merged_kind(
    life: dict[str, Any], work: dict[str, Any], now: datetime | None = None
) -> str:
    now = now or datetime.now(TZ)
    candidates: list[str] = []
    if life.get("kind") and not _ended(life, now):
        candidates.append(str(life["kind"]))
    if work.get("kind") and not _ended(work, now):
        candidates.append(str(work["kind"]))
    if not candidates and work.get("kind"):
        candidates.append(str(work["kind"]))
    if not candidates and life.get("kind"):
        candidates.append(str(life["kind"]))
    if not candidates:
        return "free"
    for k in KIND_PRIORITY:
        if k in candidates:
            return k
    return candidates[0]


def load_active_override(
    override_path: Path, now: datetime | None = None
) -> dict[str, Any] | None:
    now = now or datetime.now(TZ)
    data = load_yaml_file(override_path)
    until_raw = data.get("until")
    if not until_raw:
        return None
    try:
        until = datetime.fromisoformat(str(until_raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=TZ)
    if until <= now:
        return None
    return data


def build_merged(
    life: dict[str, Any],
    work: dict[str, Any],
    rules: dict[str, Any],
    *,
    override: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(TZ)
    kind = pick_merged_kind(life, work, now)
    if kind in {"meeting", "focus", "default_work", "work_private"}:
        theme = work.get("theme")
    elif not _ended(life, now) and life.get("theme"):
        theme = life.get("theme")
    else:
        theme = work.get("theme") or (None if _ended(life, now) else life.get("theme"))

    life_block = {
        "kind": life.get("kind"),
        "theme": life.get("theme"),
        "place": life.get("place"),
        "family_on_duty": bool(life.get("family_on_duty")),
        "start": life.get("start"),
        "end": life.get("end"),
        "sources": life.get("sources") or ["meta-schedule"],
    }
    work_block = {
        "kind": work.get("kind"),
        "theme": work.get("theme"),
        "threads": work.get("threads") or [],
        "start": work.get("start"),
        "end": work.get("end"),
        "calendar": work.get("calendar"),
        "event_id": work.get("event_id"),
        "sources": work.get("sources") or ["work-schedule"],
    }
    sources: list[str] = []
    for s in (life.get("sources") or []) + (work.get("sources") or []):
        if s not in sources:
            sources.append(s)
    if "meta-schedule" not in sources and life:
        sources.append("meta-schedule")
    if "work-schedule" not in sources and work:
        sources.append("work-schedule")
    sources.append("reachability-rules")

    override_active = bool(override)
    actual = work.get("actual") or life.get("actual") or {
        "theme": theme,
        "confidence": "low",
        "source": "scheduled_not_observed",
    }
    reachability = reachability_for(kind, rules)
    energy = work.get("energy") or life.get("energy") or {
        "mode": "unknown",
        "source": "scheduled",
    }
    location = {
        "place": life.get("place") or ("home_office" if work.get("kind") else None),
        "label": None,
        "source": "meta-schedule" if life.get("place") else "kind_default",
    }
    incident_mode = False
    if override:
        if override.get("actual_theme"):
            theme = override["actual_theme"]
            actual = {
                "theme": theme,
                "confidence": "high",
                "source": "override",
            }
        if isinstance(override.get("reachability"), dict):
            reachability = dict(override["reachability"])
        if isinstance(override.get("energy"), dict):
            energy = dict(override["energy"])
            energy.setdefault("source", "override")
        if isinstance(override.get("location"), dict):
            location = dict(override["location"])
            location.setdefault("source", "override")
        incident_mode = bool(override.get("incident_mode"))
        if "override" not in sources:
            sources.append("override")

    return {
        "okf_type": "Note",
        "type": "optima",
        "status": "active",
        "title": "Optima — current",
        "description": f"Optima now: {kind} — {theme or 'free'}",
        "schedule_role": "current",
        "optima_version": "0.9.0",
        "kind": kind,
        "theme": theme,
        "scheduled": {
            "kind": kind,
            "theme": theme,
            "start": work.get("start") or life.get("start"),
            "end": work.get("end") or life.get("end"),
            "threads": work.get("threads") or [],
            "event_id": work.get("event_id"),
            "calendar": work.get("calendar") or "personal",
            "summary": work.get("summary") or life.get("theme"),
        },
        "actual": actual,
        "meeting": None,
        "location": location,
        "reachability": reachability,
        "energy": energy,
        "family": {
            "on_duty": bool(life.get("family_on_duty")),
            "notes": None,
        },
        "incident_mode": incident_mode,
        "projections": {
            "likely_asleep_until": None,
            "commute_buffer_min": None,
            "next_start": work.get("next_start") or life.get("next_start"),
            "next_theme": work.get("next_theme") or life.get("next_theme"),
        },
        "life": life_block,
        "work": work_block,
        "horizon_path": "horizon.md",
        "recent_path": "recent.md",
        "override_active": override_active,
        "sources": sources,
        "synced_at": now.isoformat(),
        "timestamp": now.isoformat(),
        "writer": "apo_engine",
    }


def degraded_free(*, now: datetime | None = None) -> dict[str, Any]:
    """Habits/manual-only desk: valid current when all domain sources missing."""
    now = now or datetime.now(TZ)
    return build_merged({}, {}, {}, override=None, now=now)


def _path_under_root(root: Path, rel: str) -> Path | None:
    """Resolve ``rel`` under ``root``; reject ``..`` escapes. Absolute/~/ paths None."""
    raw = rel.strip()
    if not raw or raw.startswith(("~", "/")):
        return None
    root_r = root.expanduser().resolve()
    candidate = (root_r / raw.lstrip("/")).resolve()
    try:
        candidate.relative_to(root_r)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _resolve_source_path(
    vault_root: Path, spec: SourceSpec, bindings: dict[str, vaults.VaultBinding]
) -> Path | None:
    """Resolve a contract source to a filesystem path; None if unavailable."""
    raw = spec.path.strip()
    # Absolute / ~ paths: allowed only as explicit contract escape (operator-authored).
    if raw.startswith(("~", "/")):
        p = Path(raw).expanduser().resolve()
        return p if p.is_file() else None

    vault_id = (spec.vault or "").strip()
    if vault_id:
        # Alias: meta ↔ atlas (desk registries vary)
        candidates = [vault_id]
        if vault_id == "meta":
            candidates.append("atlas")
        elif vault_id == "atlas":
            candidates.append("meta")
        binding = None
        for name in candidates:
            binding = bindings.get(name)
            if binding is not None:
                break
        if binding is None:
            return None
        return _path_under_root(binding.root, raw)

    return _path_under_root(vault_root, raw)


def _load_schedule_file(path: Path) -> dict[str, Any]:
    return parse_md_frontmatter(path)


_VOLATILE_KEYS = frozenset({"synced_at", "timestamp"})


def _stable_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Drop wall-clock stamps so idle ticks do not rewrite current.yaml."""
    return {k: v for k, v in data.items() if k not in _VOLATILE_KEYS}


def _payload_changed(prev_text: str | None, merged: dict[str, Any]) -> bool:
    if not prev_text:
        return True
    try:
        prev = yaml.safe_load(prev_text) or {}
    except yaml.YAMLError:
        return True
    if not isinstance(prev, dict):
        return True
    return _stable_payload(prev) != _stable_payload(merged)


def run_merge(
    vault_root: Path,
    *,
    dry_run: bool = False,
    settings: MergeSettings | None = None,
) -> dict[str, Any]:
    """Merge domain schedules into Optima ``current.yaml``.

    Returns a summary dict with ``ok``, ``kind``, ``theme``, ``wrote``, ``degraded``.
    """
    vault_root = vault_root.expanduser().resolve()
    settings = settings or optima_contract.merge_settings(vault_root)
    if optima_contract.merge_opted_out(settings):
        return {"ok": True, "skipped": True, "reason": "opt_out"}

    bindings_error: str | None = None
    try:
        _default, bindings = vaults.load_bindings()
    except Exception as exc:
        bindings = {}
        bindings_error = str(exc)

    life: dict[str, Any] = {}
    work: dict[str, Any] = {}
    loaded_any = False
    errors: list[str] = []

    for spec in settings.sources:
        path = _resolve_source_path(vault_root, spec, bindings)
        if path is None:
            if spec.if_missing == "error":
                errors.append(f"missing source {spec.id}: {spec.vault}:{spec.path}")
            continue
        data = _load_schedule_file(path)
        if not data:
            if spec.if_missing == "error":
                errors.append(f"empty source {spec.id}")
            continue
        loaded_any = True
        role = spec.role
        if role in {"life_presence", "life", "meta"}:
            life = data
        elif role in {"work_theme", "work"}:
            work = data
        else:
            # Unknown role: treat as work if work empty else life
            if not work:
                work = data
            elif not life:
                life = data

    if errors:
        return {"ok": False, "error": "source_error", "message": "; ".join(errors)}

    override_path = vault_root / settings.override_rel.lstrip("/")
    override = load_active_override(override_path)
    if override is None and settings.override_if_missing == "error":
        if not override_path.is_file():
            return {
                "ok": False,
                "error": "override_missing",
                "message": str(override_path),
            }

    rules = load_yaml_file(vault_root / settings.reachability_rel.lstrip("/"))
    degraded = False
    if not loaded_any and override is None:
        if settings.on_all_sources_missing == "error":
            return {
                "ok": False,
                "error": "no_sources",
                "message": "no domain schedules or override",
            }
        merged = degraded_free()
        degraded = True
    else:
        merged = build_merged(life, work, rules, override=override)

    out_path = vault_root / settings.output_current.lstrip("/")
    wrote = False
    if not dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        prev_text = out_path.read_text(encoding="utf-8") if out_path.is_file() else None
        if _payload_changed(prev_text, merged):
            content = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True)
            out_path.write_text(content, encoding="utf-8")
            wrote = True

    result: dict[str, Any] = {
        "ok": True,
        "kind": merged.get("kind"),
        "theme": merged.get("theme"),
        "wrote": wrote,
        "degraded": degraded,
        "path": str(out_path),
        "dry_run": dry_run,
    }
    if bindings_error:
        result["bindings_warning"] = bindings_error
    return result


class VaultMergeController:
    """Per-vault interval merge for ``apo-engine watch``."""

    def __init__(self, vault_root: Path, *, verbose: bool = False) -> None:
        self.root = vault_root.expanduser().resolve()
        self.verbose = verbose
        self._last_merge_at: float = 0.0
        self._lock = threading.Lock()

    def tick(self, *, index_busy: bool = False) -> None:
        if not optima_contract.merge_enabled(self.root):
            return
        if index_busy:
            return
        settings = optima_contract.merge_settings(self.root)
        now = time.monotonic()
        with self._lock:
            if self._last_merge_at == 0.0:
                # First tick: wait one interval (same pattern as git-sync idle pull)
                self._last_merge_at = now
                return
            if (now - self._last_merge_at) < settings.interval_seconds:
                return
            self._last_merge_at = now

        try:
            result = run_merge(self.root, settings=settings)
        except Exception as exc:
            if self.verbose:
                print(f"  [{self.root.name}] optima-merge error: {exc}", flush=True)
            return
        if self.verbose and result.get("ok") and result.get("wrote"):
            print(
                f"  [{self.root.name}] optima-merge: {result.get('kind')} — "
                f"{result.get('theme') or 'free'}",
                flush=True,
            )
        elif self.verbose and not result.get("ok"):
            print(
                f"  [{self.root.name}] optima-merge failed: {result.get('message')}",
                flush=True,
            )
