"""Git contract sync — debounce commit/push after Apo writes; idle pull.

Opt-in via ``sync.enabled`` in ``system/contracts/git-contract.schema.yaml``
(legacy ``system/config/`` still loaded).
Conflicts / non-ff / push reject → block and surface (no auto-resolve).
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from apo_engine import git_contract

_GIT_TIMEOUT_S = 120.0
_NOTIFY_TIMEOUT_S = 20.0
_STATUS_REL = Path(".apo") / "git-sync-status.json"
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

Action = Literal["status", "run", "pull", "clear_block"]


@dataclass(frozen=True)
class SyncSettings:
    enabled: bool = False
    debounce_seconds: float = 45.0
    pull_interval_seconds: float = 900.0
    commit_message_template: str = "apo: sync {iso_local}"
    auto_push: bool = True
    default_branch: str = "main"
    never_commit: tuple[str, ...] = ()
    remote: str = ""
    on_block_command: str = ""


def _lock_for(root: Path) -> threading.Lock:
    key = str(root.resolve())
    with _LOCKS_GUARD:
        if key not in _LOCKS:
            _LOCKS[key] = threading.Lock()
        return _LOCKS[key]


def _run_git(
    root: Path,
    *args: str,
    check: bool = False,
    timeout: float = _GIT_TIMEOUT_S,
    stdin_data: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        input=stdin_data,
    )


def sync_settings(vault_root: Path) -> SyncSettings:
    """Parse sync knobs from the live git contract (missing → disabled)."""
    data = git_contract.load_git_contract(vault_root) or {}
    sync = data.get("sync") if isinstance(data.get("sync"), dict) else {}
    never = data.get("never_commit") if isinstance(data.get("never_commit"), list) else []
    branch = str(data.get("default_branch") or "main").strip() or "main"
    remote = str(data.get("remote") or "").strip()
    try:
        debounce = float(sync.get("debounce_seconds", 45))
    except (TypeError, ValueError):
        debounce = 45.0
    try:
        pull_iv = float(sync.get("pull_interval_seconds", 900))
    except (TypeError, ValueError):
        pull_iv = 900.0
    tmpl = str(sync.get("commit_message_template") or "apo: sync {iso_local}").strip()
    return SyncSettings(
        enabled=bool(sync.get("enabled", False)),
        debounce_seconds=max(1.0, debounce),
        pull_interval_seconds=max(30.0, pull_iv),
        commit_message_template=tmpl or "apo: sync {iso_local}",
        auto_push=bool(sync.get("auto_push", True)),
        default_branch=branch,
        never_commit=tuple(str(p) for p in never if str(p).strip()),
        remote=remote,
        on_block_command=str(sync.get("on_block_command") or "").strip(),
    )


def sync_enabled(vault_root: Path) -> bool:
    if not git_contract.git_contract_active(vault_root):
        return False
    return sync_settings(vault_root).enabled


def status_path(vault_root: Path) -> Path:
    return vault_root / _STATUS_REL


def read_status(vault_root: Path) -> dict[str, Any]:
    path = status_path(vault_root)
    if not path.is_file():
        return {
            "state": "idle",
            "enabled": sync_enabled(vault_root),
            "last_commit": None,
            "last_push": None,
            "last_pull": None,
            "error": None,
            "blocked_at": None,
            "updated_at": None,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "state": "idle",
            "enabled": sync_enabled(vault_root),
            "last_commit": None,
            "last_push": None,
            "last_pull": None,
            "error": "status_unreadable",
            "blocked_at": None,
            "updated_at": None,
        }
    if not isinstance(data, dict):
        data = {}
    data.setdefault("enabled", sync_enabled(vault_root))
    return data


def write_status(vault_root: Path, status: dict[str, Any]) -> dict[str, Any]:
    path = status_path(vault_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    status = dict(status)
    status["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status["enabled"] = sync_enabled(vault_root)
    path.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return status


def is_blocked(vault_root: Path) -> bool:
    return read_status(vault_root).get("state") == "blocked"


def clear_block(vault_root: Path) -> dict[str, Any]:
    st = read_status(vault_root)
    st["state"] = "ok" if st.get("last_commit") or st.get("last_pull") else "idle"
    st["error"] = None
    st["blocked_at"] = None
    return write_status(vault_root, st)


def _notify_blocked(vault_root: Path, error: str) -> None:
    """Run the contract's ``sync.on_block_command`` hook, if configured.

    Blocked state otherwise lives only in ``.apo/git-sync-status.json`` and is
    surfaced nowhere, so a stuck vault looks healthy while backups are off.
    A failing hook must never mask the block it is reporting.
    """
    command = sync_settings(vault_root).on_block_command
    if not command:
        return
    try:
        subprocess.run(
            command,
            shell=True,
            cwd=str(vault_root),
            capture_output=True,
            text=True,
            timeout=_NOTIFY_TIMEOUT_S,
            check=False,
            env={
                **os.environ,
                "APO_VAULT_ROOT": str(vault_root),
                "APO_SYNC_ERROR": error[:800],
            },
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _block(vault_root: Path, error: str, *, st: dict[str, Any] | None = None) -> dict[str, Any]:
    status = dict(st or read_status(vault_root))
    was_blocked = status.get("state") == "blocked"
    status["state"] = "blocked"
    status["error"] = error[:800]
    status["blocked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    written = write_status(vault_root, status)
    if not was_blocked:
        _notify_blocked(vault_root, error)
    return written


def unsafe_git_state(vault_root: Path) -> str | None:
    """Return a reason string if sync must not run, else None."""
    if not git_contract.is_git_work_tree(vault_root):
        return "not a git work tree"
    proc = _run_git(vault_root, "rev-parse", "--abbrev-ref", "HEAD")
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or "rev-parse failed").strip()[:200]
    branch = (proc.stdout or "").strip()
    if branch == "HEAD":
        return "detached HEAD"
    git_dir = _run_git(vault_root, "rev-parse", "--git-dir")
    if git_dir.returncode != 0:
        return "cannot resolve .git"
    gd = Path((git_dir.stdout or "").strip())
    if not gd.is_absolute():
        gd = vault_root / gd
    for name in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        if (gd / name).exists():
            return f"in progress: {name}"
    return None


def path_never_commit(rel: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """True if vault-relative path matches a never_commit glob."""
    rel = rel.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    if not rel:
        return False
    name = Path(rel).name
    parts = rel.split("/")
    for raw in patterns:
        pat = str(raw).strip().replace("\\", "/")
        if not pat:
            continue
        if pat.endswith("/"):
            prefix = pat.rstrip("/")
            if rel == prefix or rel.startswith(prefix + "/"):
                return True
            continue
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
            return True
        if pat.startswith("**/"):
            tail = pat[3:]
            if fnmatch.fnmatch(name, tail) or fnmatch.fnmatch(rel, tail):
                return True
            for i in range(len(parts)):
                if fnmatch.fnmatch("/".join(parts[i:]), tail):
                    return True
        for i in range(len(parts)):
            if fnmatch.fnmatch("/".join(parts[i:]), pat):
                return True
    return False


def _parse_porcelain_z(out: str) -> list[str]:
    """Paths from ``git status --porcelain -z`` records.

    ``-z`` is required: the newline format applies C-style quoting to any path
    that is not plain ASCII (``core.quotePath``), so a name like ``a — b.md``
    arrives as ``"a \\342\\200\\224 b.md"`` and will not match as a pathspec.
    Rename/copy records carry the origin path in a second NUL-terminated field.
    """
    fields = (out or "").split("\0")
    paths: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        # Record is "XY PATH": two status columns (either may be a space),
        # one separator space, then the raw path.
        if len(entry) < 4:
            continue
        xy, rel = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            i += 1  # consume origin path
        if rel:
            paths.append(rel)
    return paths


def list_stageable_paths(vault_root: Path, never_commit: tuple[str, ...] | list[str]) -> list[str]:
    """Dirty tracked + untracked paths eligible for commit (respects .gitignore via git)."""
    proc = _run_git(vault_root, "status", "--porcelain", "-z", "-u", "--ignore-submodules=dirty")
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "git status failed").strip()[:500])
    paths: list[str] = []
    for rel in _parse_porcelain_z(proc.stdout):
        if path_never_commit(rel, never_commit):
            continue
        paths.append(rel)
    # stable unique
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def format_commit_message(template: str, *, now: datetime | None = None) -> str:
    """Expand ``{iso_local}`` (ET wall clock) and ``{iso_utc}``."""
    dt = now or datetime.now(ZoneInfo("America/New_York"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
    local = dt.astimezone(ZoneInfo("America/New_York"))
    iso_local = local.strftime("%Y-%m-%d %H:%M ET")
    iso_utc = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = template.replace("{iso_local}", iso_local).replace("{iso_utc}", iso_utc)
    return msg.strip() or f"apo: sync {iso_local}"


def commit_and_push(
    vault_root: Path,
    *,
    message: str | None = None,
    push: bool | None = None,
) -> dict[str, Any]:
    """Stage eligible paths, commit, optional push. Blocks on failure."""
    settings = sync_settings(vault_root)
    st = read_status(vault_root)
    reason = unsafe_git_state(vault_root)
    if reason:
        _block(vault_root, f"unsafe_git_state: {reason}", st=st)
        return {"ok": False, "error": "unsafe_git_state", "message": reason, "status": read_status(vault_root)}

    if is_blocked(vault_root):
        return {
            "ok": False,
            "error": "blocked",
            "message": st.get("error") or "git sync blocked; clear_block first",
            "status": st,
        }

    try:
        paths = list_stageable_paths(vault_root, settings.never_commit)
    except RuntimeError as e:
        _block(vault_root, str(e), st=st)
        return {"ok": False, "error": "status_failed", "message": str(e), "status": read_status(vault_root)}

    if not paths:
        st["state"] = "ok"
        st["error"] = None
        write_status(vault_root, st)
        return {"ok": True, "committed": False, "pushed": False, "paths": [], "status": read_status(vault_root)}

    # Paths go over stdin (no ARG_MAX ceiling) and literally (a name containing
    # glob or pathspec-magic characters must not be reinterpreted as a pattern).
    add = _run_git(
        vault_root,
        "--literal-pathspecs",
        "add",
        "--pathspec-from-file=-",
        "--pathspec-file-nul",
        stdin_data="\0".join(paths),
    )
    if add.returncode != 0:
        err = (add.stderr or add.stdout or "git add failed").strip()
        _block(vault_root, err, st=st)
        return {"ok": False, "error": "add_failed", "message": err[:500], "status": read_status(vault_root)}

    # Refuse if never_commit somehow staged
    cached = _run_git(vault_root, "diff", "--cached", "--name-only", "-z")
    staged = [p for p in (cached.stdout or "").split("\0") if p]
    bad = [p for p in staged if path_never_commit(p, settings.never_commit)]
    if bad:
        _run_git(
            vault_root,
            "--literal-pathspecs",
            "reset",
            "HEAD",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
            stdin_data="\0".join(bad),
        )
        _block(vault_root, f"refused never_commit paths: {bad[:5]}", st=st)
        return {
            "ok": False,
            "error": "never_commit",
            "message": f"refused to commit: {bad[:5]}",
            "status": read_status(vault_root),
        }
    if not staged:
        st["state"] = "ok"
        write_status(vault_root, st)
        return {"ok": True, "committed": False, "pushed": False, "paths": [], "status": read_status(vault_root)}

    msg = (message or "").strip() or format_commit_message(settings.commit_message_template)
    commit = _run_git(vault_root, "commit", "-m", msg)
    if commit.returncode != 0:
        err = (commit.stderr or commit.stdout or "git commit failed").strip()
        if "nothing to commit" in err.lower():
            st["state"] = "ok"
            write_status(vault_root, st)
            return {"ok": True, "committed": False, "pushed": False, "paths": [], "status": read_status(vault_root)}
        _block(vault_root, err, st=st)
        return {"ok": False, "error": "commit_failed", "message": err[:500], "status": read_status(vault_root)}

    head = _run_git(vault_root, "rev-parse", "HEAD")
    commit_hash = (head.stdout or "").strip() if head.returncode == 0 else ""
    st["last_commit"] = {
        "hash": commit_hash,
        "message": msg,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "paths": staged,
    }
    st["state"] = "ok"
    st["error"] = None
    st["blocked_at"] = None
    write_status(vault_root, st)

    do_push = settings.auto_push if push is None else bool(push)
    pushed = False
    if do_push:
        push_result = _push(vault_root, settings)
        if not push_result["ok"]:
            return push_result
        pushed = True

    return {
        "ok": True,
        "committed": True,
        "pushed": pushed,
        "hash": commit_hash,
        "message": msg,
        "paths": staged,
        "status": read_status(vault_root),
    }


def _push(vault_root: Path, settings: SyncSettings) -> dict[str, Any]:
    st = read_status(vault_root)
    # Intentionally never pass --force / --force-with-lease.
    proc = _run_git(vault_root, "push")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "git push failed").strip()
        # Try setting upstream once if missing
        if "has no upstream" in err.lower() or "no upstream" in err.lower():
            branch = settings.default_branch
            cur = _run_git(vault_root, "rev-parse", "--abbrev-ref", "HEAD")
            if cur.returncode == 0 and (cur.stdout or "").strip():
                branch = (cur.stdout or "").strip()
            proc = _run_git(vault_root, "push", "-u", "origin", branch)
            err = (proc.stderr or proc.stdout or "git push failed").strip()
            if proc.returncode != 0:
                _block(vault_root, err, st=st)
                return {"ok": False, "error": "push_failed", "message": err[:500], "status": read_status(vault_root)}
        else:
            _block(vault_root, err, st=st)
            return {"ok": False, "error": "push_failed", "message": err[:500], "status": read_status(vault_root)}

    st = read_status(vault_root)
    st["last_push"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": True,
    }
    st["state"] = "ok"
    st["error"] = None
    write_status(vault_root, st)
    return {"ok": True, "pushed": True, "status": read_status(vault_root)}


def pull_ff_only(vault_root: Path) -> dict[str, Any]:
    """``git pull --ff-only``. On failure → block."""
    settings = sync_settings(vault_root)
    st = read_status(vault_root)
    reason = unsafe_git_state(vault_root)
    if reason:
        _block(vault_root, f"unsafe_git_state: {reason}", st=st)
        return {"ok": False, "error": "unsafe_git_state", "message": reason, "status": read_status(vault_root)}
    if is_blocked(vault_root):
        return {
            "ok": False,
            "error": "blocked",
            "message": st.get("error") or "git sync blocked; clear_block first",
            "status": st,
        }

    # Dirty tree: still attempt ff-only; git will refuse if it would overwrite
    proc = _run_git(vault_root, "pull", "--ff-only")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "git pull --ff-only failed").strip()
        _block(vault_root, err, st=st)
        return {"ok": False, "error": "pull_failed", "message": err[:500], "status": read_status(vault_root)}

    st = read_status(vault_root)
    st["last_pull"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": True,
        "summary": (proc.stdout or "").strip()[:400],
    }
    st["state"] = "ok"
    st["error"] = None
    st["blocked_at"] = None
    write_status(vault_root, st)
    return {"ok": True, "pulled": True, "status": read_status(vault_root), "branch": settings.default_branch}


def run_action(
    vault_root: Path,
    action: Action,
    *,
    message: str | None = None,
) -> dict[str, Any]:
    """MCP/RPC entry: status | run | pull | clear_block."""
    with _lock_for(vault_root):
        if action == "status":
            st = read_status(vault_root)
            return {
                "ok": True,
                "action": "status",
                "sync_enabled": sync_enabled(vault_root),
                "contract_active": git_contract.git_contract_active(vault_root),
                "unsafe": unsafe_git_state(vault_root),
                "status": st,
            }
        if action == "clear_block":
            st = clear_block(vault_root)
            return {"ok": True, "action": "clear_block", "status": st}
        if not sync_enabled(vault_root) and action in ("run", "pull"):
            # Tool may still run when contract active but sync disabled? MVP: opt-in.
            # Allow explicit tool run even if sync.enabled false when contract active —
            # agents need force sync. Only require contract active.
            if not git_contract.git_contract_active(vault_root):
                return {
                    "ok": False,
                    "error": "sync_inactive",
                    "message": "git contract not active (need YAML + work tree)",
                }
        if action == "pull":
            return {**pull_ff_only(vault_root), "action": "pull"}
        if action == "run":
            return {**commit_and_push(vault_root, message=message), "action": "run"}
        return {"ok": False, "error": "bad_action", "message": f"unknown action {action!r}"}


class VaultSyncController:
    """Per-vault debounce + idle pull for ``apo-engine watch``."""

    def __init__(self, vault_root: Path, *, verbose: bool = False) -> None:
        self.root = vault_root
        self.verbose = verbose
        self._commit_due_at: float | None = None
        self._last_pull_at: float = 0.0
        self._lock = threading.Lock()

    def note_apo_writes(self) -> None:
        if not sync_enabled(self.root):
            return
        settings = sync_settings(self.root)
        with self._lock:
            self._commit_due_at = time.monotonic() + settings.debounce_seconds

    def pending_commit(self) -> bool:
        with self._lock:
            return self._commit_due_at is not None

    def tick(self, *, index_busy: bool = False) -> None:
        if not sync_enabled(self.root):
            return
        if is_blocked(self.root):
            return
        settings = sync_settings(self.root)
        now = time.monotonic()

        due_commit = False
        with self._lock:
            if self._commit_due_at is not None and now >= self._commit_due_at:
                due_commit = True
                self._commit_due_at = None

        if due_commit:
            with _lock_for(self.root):
                result = commit_and_push(self.root)
            if self.verbose:
                if result.get("ok") and result.get("committed"):
                    print(
                        f"  git-sync commit: {result.get('hash', '')[:8]} "
                        f"{result.get('message')!r}",
                        flush=True,
                    )
                elif not result.get("ok"):
                    print(f"  git-sync blocked: {result.get('message')}", flush=True)
            return

        # Idle pull: no pending commit debounce, index not busy
        with self._lock:
            commit_waiting = self._commit_due_at is not None
        if commit_waiting or index_busy:
            return
        if self._last_pull_at and (now - self._last_pull_at) < settings.pull_interval_seconds:
            return
        # First tick: don't pull immediately — wait one interval from start
        if self._last_pull_at == 0.0:
            self._last_pull_at = now
            return
        self._last_pull_at = now
        with _lock_for(self.root):
            result = pull_ff_only(self.root)
        if self.verbose and not result.get("ok"):
            print(f"  git-sync pull blocked: {result.get('message')}", flush=True)
        elif self.verbose and result.get("ok"):
            print("  git-sync pull: ok", flush=True)
