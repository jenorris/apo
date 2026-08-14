"""Archival contract — suggest-mode eligibility for cold notes.

Active when ``system/contracts/archival-contract.schema.yaml`` (or legacy
``system/config/archival-contract.schema.yaml``) exists under the vault root.

Runtime (v1): ``mode: suggest`` emits structured ``flaws[]`` via
``vault(action=lint)`` and post-write checks. ``mode: auto`` is treated as
``off`` (tip on lint). Destination strategy ``mirror`` only.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml

from apo_engine.okf import path_glob_match

ARCHIVAL_CONTRACT_CANDIDATES = (
    Path("system") / "contracts" / "archival-contract.schema.yaml",
    Path("system") / "config" / "archival-contract.schema.yaml",
)
ARCHIVAL_CONTRACT_REL = ARCHIVAL_CONTRACT_CANDIDATES[0]

AUTO_TIP = "archival mode auto not implemented; treated as off"

# Post-write may emit these; blocked_status is lint-only.
WRITE_PATH_CODES = frozenset({"archive.eligible", "archive.blocked_todos"})

FlawScope = Literal["write", "lint"]


def resolve_archival_contract_path(
    vault_root: Path, explicit: str | None = None
) -> Path | None:
    if explicit is None:
        explicit = os.environ.get("APO_ARCHIVAL_CONTRACT", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    for rel in ARCHIVAL_CONTRACT_CANDIDATES:
        candidate = vault_root / rel
        if candidate.is_file():
            return candidate
    return None


def load_archival_contract(
    vault_root: Path, explicit: str | None = None
) -> dict[str, Any] | None:
    """Parse archival-contract YAML if present. Returns None when missing/unreadable."""
    path = resolve_archival_contract_path(vault_root, explicit)
    if path is None:
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def raw_mode(data: dict[str, Any] | None) -> str:
    if not data:
        return "off"
    raw = str(data.get("mode") or "suggest").strip().lower()
    if raw in {"off", "suggest", "auto"}:
        return raw
    return "off"


def effective_mode(data: dict[str, Any] | None) -> Literal["suggest", "off"]:
    """Runtime mode: only ``suggest`` emits findings; ``auto`` → ``off``."""
    return "suggest" if raw_mode(data) == "suggest" else "off"


def destination_for(rel_path: str, data: dict[str, Any]) -> str | None:
    """Return mirror destination under archives/, or None if unsupported strategy."""
    dest = data.get("destination") if isinstance(data.get("destination"), dict) else {}
    root = str(dest.get("root") or "archives").strip().strip("/") or "archives"
    strategy = str(dest.get("strategy") or "mirror").strip().lower()
    if strategy != "mirror":
        return None
    rel = rel_path.replace("\\", "/").lstrip("/")
    if rel.startswith(root + "/") or rel == root:
        return None
    return f"{root}/{rel}"


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if isinstance(x, str) and str(x).strip()]


def _folder_prefix_match(rel: str, folder: str) -> bool:
    folder = folder.replace("\\", "/").strip().strip("/")
    if not folder:
        return True
    return rel == folder or rel.startswith(folder + "/")


def _in_include(rel: str, include_folders: list[str]) -> bool:
    if not include_folders:
        return True
    return any(_folder_prefix_match(rel, f) for f in include_folders)


def _is_exempt(rel: str, exempt_folders: list[str], exempt_globs: list[str]) -> bool:
    if any(_folder_prefix_match(rel, f) for f in exempt_folders):
        return True
    return any(path_glob_match(rel, g) for g in exempt_globs)


def _parse_idle_instant(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # Normalize common vault forms
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Space-separated datetime → ISO-ish
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(raw.strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _idle_info(
    full: Path,
    fm: dict[str, Any],
    idle_cfg: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[bool, str, str | None]:
    """Return (is_idle, field_used, value_repr)."""
    now = now or datetime.now(timezone.utc)
    field = str(idle_cfg.get("field") or "last_activity").strip().lower()
    try:
        days = float(idle_cfg.get("older_than_days") or 90)
    except (TypeError, ValueError):
        days = 90.0
    threshold_secs = max(0.0, days) * 86400.0

    if field == "mtime":
        try:
            mtime = full.stat().st_mtime
        except OSError:
            return False, "mtime", None
        age = now.timestamp() - mtime
        return age >= threshold_secs, "mtime", datetime.fromtimestamp(
            mtime, tz=timezone.utc
        ).isoformat()

    # Prefer last_activity; fall back to mtime when missing/unparseable
    la_raw = fm.get("last_activity")
    instant = _parse_idle_instant(la_raw)
    if instant is not None:
        age = (now - instant).total_seconds()
        return age >= threshold_secs, "last_activity", str(la_raw)

    try:
        mtime = full.stat().st_mtime
    except OSError:
        return False, "mtime", None
    age = now.timestamp() - mtime
    return age >= threshold_secs, "mtime", datetime.fromtimestamp(
        mtime, tz=timezone.utc
    ).isoformat()


def _has_open_todos(fm: dict[str, Any]) -> bool:
    todos = fm.get("todos")
    if not isinstance(todos, list):
        return False
    for item in todos:
        if not isinstance(item, dict):
            continue
        st = str(item.get("status") or "").strip().lower()
        if st in {"pending", "open", "todo", "in-progress", "in_progress", "blocked"}:
            return True
    return False


def _set_fields_evidence(data: dict[str, Any]) -> dict[str, Any]:
    actions = data.get("actions") if isinstance(data.get("actions"), dict) else {}
    raw = actions.get("set_fields") if isinstance(actions.get("set_fields"), dict) else {}
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        if v == "$now":
            out[k] = None
        else:
            out[k] = v
    if not out:
        out = {"status": "archived", "archived_at": None}
    return out


def _okf_type_allowed(fm: dict[str, Any], eligibility: dict[str, Any]) -> bool:
    allow = _str_list(eligibility.get("okf_type_in"))
    if not allow:
        return True
    val = fm.get("okf_type")
    if val is None:
        val = fm.get("type")
    if val is None:
        return False
    return str(val).strip() in allow


def evaluate_path(
    vault_root: Path,
    rel_path: str,
    data: dict[str, Any],
    *,
    scope: FlawScope = "lint",
    now: datetime | None = None,
    frontmatter: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return one flaw dict or None.

    ``scope=write`` suppresses ``archive.blocked_status``.
    """
    if effective_mode(data) != "suggest":
        return None

    rel = rel_path.replace("\\", "/").lstrip("/")
    if not rel.endswith((".md", ".markdown")):
        return None

    eligibility = (
        data.get("eligibility") if isinstance(data.get("eligibility"), dict) else {}
    )
    include_folders = _str_list(eligibility.get("include_folders"))
    exempt_folders = _str_list(eligibility.get("exempt_folders"))
    exempt_globs = _str_list(eligibility.get("exempt_globs"))
    status_in = {s.lower() for s in _str_list(eligibility.get("status_in"))}
    idle_cfg = (
        eligibility.get("idle") if isinstance(eligibility.get("idle"), dict) else {}
    )
    safety = data.get("safety") if isinstance(data.get("safety"), dict) else {}
    deny_todos = bool(safety.get("deny_if_open_todos", True))

    if not _in_include(rel, include_folders):
        return None
    if _is_exempt(rel, exempt_folders, exempt_globs):
        return None

    full = (vault_root / rel).resolve()
    try:
        full.relative_to(vault_root.resolve())
    except ValueError:
        return None
    if not full.is_file():
        return None

    if frontmatter is None:
        from apo_engine import core

        try:
            text = full.read_text(encoding="utf-8")
        except OSError:
            return None
        frontmatter = core.note_frontmatter(text, rel) or {}

    fm = frontmatter
    if not _okf_type_allowed(fm, eligibility):
        return None

    is_idle, idle_field, idle_value = _idle_info(full, fm, idle_cfg, now=now)
    if not is_idle:
        return None

    status_raw = fm.get("status")
    status = str(status_raw).strip().lower() if status_raw is not None else ""
    status_ok = bool(status) and status in status_in

    if not status_ok:
        if scope == "write":
            return None
        return {
            "code": "archive.blocked_status",
            "severity": "info",
            "path": rel,
            "evidence": {
                "status": status_raw,
                "idle_field": idle_field,
                "idle_value": idle_value,
                "status_in": sorted(status_in),
            },
            "remediation": "llm",
            "message": (
                f"idle ({idle_field}) but status "
                f"{status_raw!r} not in archival status_in"
            ),
        }

    dst = destination_for(rel, data)
    if dst is None:
        return None

    if deny_todos and _has_open_todos(fm):
        return {
            "code": "archive.blocked_todos",
            "severity": "warn",
            "path": rel,
            "evidence": {
                "status": status_raw,
                "idle_field": idle_field,
                "idle_value": idle_value,
                "set_fields": _set_fields_evidence(data),
            },
            "remediation": "llm",
            "suggested_op": {
                "tool": "patch_note",
                "ops": [{"op": "place", "src": rel, "dst": dst}],
            },
            "message": (
                "eligible for archive except open todos; resolve todos, "
                "then set_field on src then place"
            ),
        }

    return {
        "code": "archive.eligible",
        "severity": "info",
        "path": rel,
        "evidence": {
            "status": status_raw,
            "idle_field": idle_field,
            "idle_value": idle_value,
            "set_fields": _set_fields_evidence(data),
        },
        "remediation": "llm",
        "suggested_op": {
            "tool": "patch_note",
            "ops": [{"op": "place", "src": rel, "dst": dst}],
        },
        "message": (
            f"eligible for archive (idle + status {status_raw}); "
            "set_field on src then place"
        ),
    }


def iter_candidate_paths(
    vault_root: Path,
    data: dict[str, Any],
    *,
    folder: str = "",
) -> list[str]:
    """Markdown paths under include_folders (or folder= override), minus exempt."""
    eligibility = (
        data.get("eligibility") if isinstance(data.get("eligibility"), dict) else {}
    )
    include_folders = _str_list(eligibility.get("include_folders"))
    exempt_folders = _str_list(eligibility.get("exempt_folders"))
    exempt_globs = _str_list(eligibility.get("exempt_globs"))
    folder_clean = folder.replace("\\", "/").strip().strip("/")

    roots: list[Path]
    if folder_clean:
        roots = [vault_root / folder_clean]
    elif include_folders:
        roots = [vault_root / f for f in include_folders]
    else:
        roots = [vault_root]

    found: list[str] = []
    root_res = vault_root.resolve()
    for base in roots:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".md", ".markdown"}:
                continue
            try:
                rel = path.resolve().relative_to(root_res).as_posix()
            except ValueError:
                continue
            if folder_clean and not _folder_prefix_match(rel, folder_clean):
                continue
            if not folder_clean and not _in_include(rel, include_folders):
                continue
            if _is_exempt(rel, exempt_folders, exempt_globs):
                continue
            found.append(rel)
    return found


def lint_vault(
    vault_root: Path,
    data: dict[str, Any] | None,
    *,
    folder: str = "",
    limit: int = 50,
    offset: int = 0,
    vault_name: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Sweep eligible notes; return flaws + pagination metadata."""
    out: dict[str, Any] = {
        "ok": True,
        "flaws": [],
        "count_by_code": {},
        "has_more": False,
        "vault": vault_name,
    }
    if data is None:
        return out

    mode_raw = raw_mode(data)
    if mode_raw == "auto":
        out["tip"] = AUTO_TIP

    if effective_mode(data) != "suggest":
        return out

    dest = data.get("destination") if isinstance(data.get("destination"), dict) else {}
    strategy = str(dest.get("strategy") or "mirror").strip().lower()
    if strategy != "mirror":
        out["warning"] = (
            f"archival destination strategy {strategy!r} unsupported in v1 "
            "(mirror only); no eligible flaws"
        )

    limit = max(0, int(limit))
    offset = max(0, int(offset))
    paths = iter_candidate_paths(vault_root, data, folder=folder)
    collected: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for rel in paths:
        flaw = evaluate_path(
            vault_root, rel, data, scope="lint", now=now
        )
        if flaw is None:
            continue
        code = str(flaw.get("code") or "")
        counts[code] = counts.get(code, 0) + 1
        collected.append(flaw)

    sliced = collected[offset : offset + limit] if limit else []
    out["flaws"] = sliced
    out["count_by_code"] = counts
    out["has_more"] = (offset + len(sliced)) < len(collected)
    out["total"] = len(collected)
    return out


def evaluate_write_path(
    vault_root: Path,
    rel_path: str,
    *,
    content: str | None = None,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Post-write check. Returns (flaws, optional tip for auto mode)."""
    data = load_archival_contract(vault_root)
    if data is None:
        return [], None
    tip = AUTO_TIP if raw_mode(data) == "auto" else None
    if effective_mode(data) != "suggest":
        return [], tip

    fm = None
    if content is not None:
        from apo_engine import core

        fm = core.note_frontmatter(content, rel_path) or {}

    flaw = evaluate_path(
        vault_root,
        rel_path,
        data,
        scope="write",
        now=now,
        frontmatter=fm,
    )
    if flaw is None:
        return [], tip
    if flaw.get("code") not in WRITE_PATH_CODES:
        return [], tip
    return [flaw], tip


def clear_archival_contract_cache() -> None:
    """Reserved; currently no-op (loads are uncached)."""
    return None
