"""Git contract sync — debounce commit/push after Apo writes; idle pull.

Opt-in via ``sync.enabled`` in ``system/contracts/git-contract.schema.yaml``
(legacy ``system/config/`` still loaded).
Conflicts / non-ff / push reject → block and surface (no auto-resolve).

``pull_ff_only`` blocks the moment local commits exist that the remote
doesn't have (e.g. another session's sync won the race) — by design, it
never merges. ``rebase_onto_remote`` is the explicit recovery for exactly
that case: it replays local commits on top of the remote tip so a plain
``git push`` afterward stays a fast-forward. It still never auto-resolves
content — a rebase conflict aborts back to the pre-rebase state and blocks,
the same as any other sync failure.

Transient network failures (DNS, connect refused/timeout, dropped link) are
*not* blocks: they put the vault in ``offline`` instead. Local commits keep
landing, and ``VaultSyncController.tick`` retries with exponential backoff
(60s × 2^n, capped at ``pull_interval_seconds``) — pull ff-only, then push any
backlog. A ``blocked`` status whose stored error is a transient network error
(written before this distinction existed) self-heals to ``offline``.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from apo_engine import git_contract, vaults

# Child of the "apo" logger `watch.py` configures with a rotating, timestamped
# handler — no separate wiring needed here (see `watch._configure_logging`).
# These `tick()` calls run inside the watch loop with `verbose=True` in
# production, so a bare `print()` here would bypass that handler entirely and
# keep writing raw, un-timestamped lines straight to stdout.
logger = logging.getLogger("apo.git_sync")

_GIT_TIMEOUT_S = 120.0
# push/pull/fetch touch the network — a dead remote link should fail fast
# rather than hold the local-only default's generous ceiling. 120s of a
# stalled watch loop per hang (per the `docs/watcher-scheduler-separation.md`
# review) is too long for what's really just an "is the network up" check.
_GIT_NETWORK_TIMEOUT_S = 30.0
_NOTIFY_TIMEOUT_S = 20.0
# Offline retry backoff: 60s × 2^n, capped at the vault's pull interval.
_OFFLINE_BASE_DELAY_S = 60.0
# Abort a transfer that stalls below 1 KB/s for 20s instead of waiting out the
# whole network timeout.
_NETWORK_GIT_CONFIG = ("-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=20")
# Substrings of git/curl/ssh stderr that mean "the remote is unreachable right
# now", not "the remote said no". Matched case-insensitively.
_TRANSIENT_NETWORK_MARKERS = (
    "could not resolve host",
    "failed to connect",
    "couldn't connect to server",
    "connection timed out",
    "operation timed out",
    "connection refused",
    "network is unreachable",
    "the remote end hung up unexpectedly",
    "early eof",
    "ssl_error_syscall",
    "ssh: connect to host",
)
# A real refusal can share stderr with a transient marker (e.g. "hung up"
# after an auth failure) — these always win and keep the block.
_BLOCKING_MARKERS = (
    "authentication failed",
    "permission denied",
    "could not read username",
    "could not read password",
    "repository not found",
    "non-fast-forward",
    "[rejected]",
    "not possible to fast-forward",
    "diverging branches",
    "would be overwritten",
)
_STATUS_REL = Path(".apo") / "git-sync-status.json"
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

Action = Literal["status", "run", "pull", "rebase", "clear_block"]


@dataclass(frozen=True)
class SyncSettings:
    enabled: bool = False
    debounce_seconds: float = 45.0
    pull_interval_seconds: float = 900.0
    commit_message_template: str = (
        "apo: sync {iso_local} · {path_count} paths · {top_folders}"
    )
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
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        input=stdin_data,
        env=env,
    )


def _run_git_network(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a remote-touching git op (push/pull/fetch) that never hangs or prompts.

    A timeout comes back as a failed ``CompletedProcess`` whose stderr the
    transient classifier recognises, so callers handle it like any other
    network failure instead of letting ``TimeoutExpired`` escape the watch loop.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        return _run_git(
            root, *_NETWORK_GIT_CONFIG, *args, timeout=_GIT_NETWORK_TIMEOUT_S, env=env
        )
    except subprocess.TimeoutExpired:
        op = args[0] if args else "git"
        return subprocess.CompletedProcess(
            ["git", "-C", str(root), *args],
            returncode=124,
            stdout="",
            stderr=f"git {op}: Operation timed out after {_GIT_NETWORK_TIMEOUT_S:.0f}s",
        )


def _is_transient_network_error(err: str | None) -> bool:
    """True if ``err`` means the remote was unreachable (retry), not refused (block)."""
    text = (err or "").lower()
    if not text or any(m in text for m in _BLOCKING_MARKERS):
        return False
    return any(m in text for m in _TRANSIENT_NETWORK_MARKERS)


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
    _default_tmpl = "apo: sync {iso_local} · {path_count} paths · {top_folders}"
    tmpl = str(sync.get("commit_message_template") or _default_tmpl).strip()
    return SyncSettings(
        enabled=bool(sync.get("enabled", False)),
        debounce_seconds=max(1.0, debounce),
        pull_interval_seconds=max(30.0, pull_iv),
        commit_message_template=tmpl or _default_tmpl,
        auto_push=bool(sync.get("auto_push", True)),
        default_branch=branch,
        never_commit=tuple(str(p) for p in never if str(p).strip()),
        remote=remote,
        on_block_command=str(sync.get("on_block_command") or "").strip(),
    )


def _is_read_only_vault(vault_root: Path) -> bool:
    """True if ``vault_root`` is registered with ``read_only: true``.

    A read-only mount (e.g. an ingested foreign OKF bundle) promises every
    *write* op fails — that promise must also cover the automation this
    module runs unattended (commit/push, and shelling out to
    ``on_block_command``), not just the MCP write-tool boundary.
    """
    try:
        resolved = vault_root.expanduser().resolve()
    except OSError:
        resolved = vault_root
    try:
        _, bindings = vaults.load_bindings()
    except Exception:
        return False
    for binding in bindings.values():
        if binding.root == resolved:
            return binding.read_only
    return False


def sync_enabled(vault_root: Path) -> bool:
    if not git_contract.git_contract_active(vault_root):
        return False
    if _is_read_only_vault(vault_root):
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clear_offline(status: dict[str, Any]) -> None:
    status["offline_since"] = None
    status["retry_count"] = 0
    status["next_retry_at"] = None


def clear_block(vault_root: Path) -> dict[str, Any]:
    st = read_status(vault_root)
    st["state"] = "ok" if st.get("last_commit") or st.get("last_pull") else "idle"
    st["error"] = None
    st["blocked_at"] = None
    _clear_offline(st)
    return write_status(vault_root, st)


def offline_backoff_seconds(retry_count: int, cap_seconds: float) -> float:
    """Delay before offline retry ``retry_count`` (0-based): 60s × 2^n, capped."""
    n = max(0, int(retry_count))
    # Clamp the exponent so a weeks-long outage can't overflow the float.
    return min(_OFFLINE_BASE_DELAY_S * (2 ** min(n, 32)), cap_seconds)


def offline_retry_due(status: dict[str, Any], *, now: datetime | None = None) -> bool:
    """True if ``status`` is offline and its ``next_retry_at`` has passed."""
    if status.get("state") != "offline":
        return False
    raw = status.get("next_retry_at")
    if not raw:
        return True
    try:
        due = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return (now or _utcnow()) >= due


def _defer_offline(
    vault_root: Path, error: str, *, st: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Record a transient network failure as ``offline`` and schedule a retry.

    Unlike ``_block`` this never fires ``on_block_command``: an outage is
    expected to heal on its own, and sync keeps committing locally meanwhile.
    """
    status = dict(st or read_status(vault_root))
    was_offline = status.get("state") == "offline"
    n = int(status.get("retry_count") or 0) if was_offline else 0
    now = _utcnow()
    delay = offline_backoff_seconds(n, sync_settings(vault_root).pull_interval_seconds)
    status["state"] = "offline"
    status["error"] = error[:800]
    status["blocked_at"] = None
    if not (was_offline and status.get("offline_since")):
        status["offline_since"] = now.isoformat(timespec="seconds")
    status["retry_count"] = n + 1
    status["next_retry_at"] = (now + timedelta(seconds=delay)).isoformat(timespec="seconds")
    return write_status(vault_root, status)


def _heal_transient_block(vault_root: Path, st: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a legacy ``blocked`` status caused by a network blip to ``offline``.

    Returns the rewritten status, or ``None`` if the block is a real one
    (conflict, auth, non-ff, …) and must stay put until ``clear_block``.
    """
    if st.get("state") != "blocked" or not _is_transient_network_error(st.get("error")):
        return None
    status = dict(st)
    status["state"] = "offline"
    status["offline_since"] = status.get("blocked_at") or _utcnow().isoformat(timespec="seconds")
    status["blocked_at"] = None
    status["retry_count"] = 0
    status["next_retry_at"] = _utcnow().isoformat(timespec="seconds")
    return write_status(vault_root, status)


def _network_failure(
    vault_root: Path, err: str, error_code: str, *, st: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Route a failed push/pull/fetch: transient → offline, anything else → block."""
    if _is_transient_network_error(err):
        _defer_offline(vault_root, err, st=st)
        return {"ok": False, "error": "offline", "message": err[:500], "status": read_status(vault_root)}
    _block(vault_root, err, st=st)
    return {"ok": False, "error": error_code, "message": err[:500], "status": read_status(vault_root)}


def _settle_local_ok(status: dict[str, Any]) -> None:
    """Mark a local-only success ``ok`` — unless offline, which only the remote clears."""
    if status.get("state") == "offline":
        return
    status["state"] = "ok"
    status["error"] = None
    status["blocked_at"] = None


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
    git_dir = _run_git(vault_root, "rev-parse", "--git-dir")
    if git_dir.returncode != 0:
        return "cannot resolve .git"
    gd = Path((git_dir.stdout or "").strip())
    if not gd.is_absolute():
        gd = vault_root / gd
    # A rebase is in progress only while git's own state dir exists. REBASE_HEAD
    # alone is not a signal: git leaves it behind after a conflicted rebase
    # finishes (`rebase --continue`), which would block sync forever.
    for name in ("rebase-merge", "rebase-apply"):
        if (gd / name).is_dir():
            return f"in progress: rebase ({name})"
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        if (gd / name).exists():
            return f"in progress: {name}"
    proc = _run_git(vault_root, "rev-parse", "--abbrev-ref", "HEAD")
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or "rev-parse failed").strip()[:200]
    branch = (proc.stdout or "").strip()
    if branch == "HEAD":
        return "detached HEAD"
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


_PATHS_BODY_MAX = 40
_TOP_FOLDERS_MAX = 3


def _folder_key(path: str) -> str:
    """First two path segments (``areas/threads``), or one if shallow."""
    parts = [p for p in path.replace("\\", "/").split("/") if p and p != "."]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    if len(parts) == 1:
        return parts[0]
    return "."


def paths_summary_bits(paths: list[str] | tuple[str, ...]) -> dict[str, str]:
    """Mechanical folder summary for commit-message templates."""
    counts: Counter[str] = Counter(_folder_key(p) for p in paths)
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    top = ordered[:_TOP_FOLDERS_MAX]
    folders = ", ".join(name for name, _ in top)
    extra = len(ordered) - len(top)
    if extra > 0:
        folders = f"{folders} +{extra} more" if folders else f"+{extra} more"
    n = str(len(paths))
    return {
        "path_count": n,
        "top_folders": folders,
        "paths_summary": folders,
    }


def paths_body(
    paths: list[str] | tuple[str, ...],
    *,
    max_lines: int = _PATHS_BODY_MAX,
) -> str:
    """Commit body trailer listing changed paths (capped)."""
    lines = [f"- {p}" for p in paths]
    if len(lines) > max_lines:
        rest = len(lines) - max_lines
        lines = lines[:max_lines] + [f"… and {rest} more"]
    return "Paths:\n" + "\n".join(lines)


def format_commit_message(
    template: str,
    *,
    now: datetime | None = None,
    paths: list[str] | tuple[str, ...] = (),
) -> str:
    """Expand time + path placeholders in ``sync.commit_message_template``.

    Known tokens: ``{iso_local}``, ``{iso_utc}``, ``{path_count}``,
    ``{top_folders}``, ``{paths_summary}`` (alias of top_folders).
    Unknown ``{…}`` tokens are left literal.
    """
    dt = now or datetime.now(ZoneInfo("America/New_York"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
    local = dt.astimezone(ZoneInfo("America/New_York"))
    iso_local = local.strftime("%Y-%m-%d %H:%M ET")
    iso_utc = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    bits = paths_summary_bits(paths)
    msg = (
        template.replace("{iso_local}", iso_local)
        .replace("{iso_utc}", iso_utc)
        .replace("{path_count}", bits["path_count"])
        .replace("{top_folders}", bits["top_folders"])
        .replace("{paths_summary}", bits["paths_summary"])
    )
    return msg.strip() or f"apo: sync {iso_local}"


def commit_message_parts(
    template: str,
    *,
    message: str | None = None,
    paths: list[str] | tuple[str, ...] = (),
    now: datetime | None = None,
) -> tuple[str, str]:
    """Subject (agent message or template) + Paths body trailer."""
    agent = (message or "").strip()
    if agent:
        subject = agent
    else:
        subject = format_commit_message(template, now=now, paths=paths)
    return subject, paths_body(paths)


def commit_and_push(
    vault_root: Path,
    *,
    message: str | None = None,
    push: bool | None = None,
) -> dict[str, Any]:
    """Stage eligible paths, commit, optional push. Blocks on failure.

    Offline (transient network failure) does not stop local commits; the push
    is deferred until the scheduled retry.
    """
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
        _settle_local_ok(st)
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
        _settle_local_ok(st)
        write_status(vault_root, st)
        return {"ok": True, "committed": False, "pushed": False, "paths": [], "status": read_status(vault_root)}

    subject, body = commit_message_parts(
        settings.commit_message_template,
        message=message,
        paths=staged,
    )
    commit = _run_git(vault_root, "commit", "-m", subject, "-m", body)
    if commit.returncode != 0:
        err = (commit.stderr or commit.stdout or "git commit failed").strip()
        if "nothing to commit" in err.lower():
            _settle_local_ok(st)
            write_status(vault_root, st)
            return {"ok": True, "committed": False, "pushed": False, "paths": [], "status": read_status(vault_root)}
        _block(vault_root, err, st=st)
        return {"ok": False, "error": "commit_failed", "message": err[:500], "status": read_status(vault_root)}

    head = _run_git(vault_root, "rev-parse", "HEAD")
    commit_hash = (head.stdout or "").strip() if head.returncode == 0 else ""
    st["last_commit"] = {
        "hash": commit_hash,
        "message": subject,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "paths": staged,
    }
    _settle_local_ok(st)
    write_status(vault_root, st)

    do_push = settings.auto_push if push is None else bool(push)
    pushed = False
    if do_push and st.get("state") == "offline" and not offline_retry_due(st):
        # Offline and backing off: keep the commit local; tick() pushes the
        # backlog once a retry reaches the remote.
        return {
            "ok": True,
            "committed": True,
            "pushed": False,
            "push_deferred": "offline",
            "hash": commit_hash,
            "message": subject,
            "paths": staged,
            "status": read_status(vault_root),
        }
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
        "message": subject,
        "paths": staged,
        "status": read_status(vault_root),
    }


def _push(vault_root: Path, settings: SyncSettings) -> dict[str, Any]:
    st = read_status(vault_root)
    # Intentionally never pass --force / --force-with-lease.
    proc = _run_git_network(vault_root, "push")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "git push failed").strip()
        # Try setting upstream once if missing
        if "has no upstream" in err.lower() or "no upstream" in err.lower():
            branch = settings.default_branch
            cur = _run_git(vault_root, "rev-parse", "--abbrev-ref", "HEAD")
            if cur.returncode == 0 and (cur.stdout or "").strip():
                branch = (cur.stdout or "").strip()
            proc = _run_git_network(vault_root, "push", "-u", "origin", branch)
            err = (proc.stderr or proc.stdout or "git push failed").strip()
            if proc.returncode != 0:
                return _network_failure(vault_root, err, "push_failed", st=st)
        else:
            return _network_failure(vault_root, err, "push_failed", st=st)

    st = read_status(vault_root)
    st["last_push"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": True,
    }
    st["state"] = "ok"
    st["error"] = None
    st["blocked_at"] = None
    _clear_offline(st)
    write_status(vault_root, st)
    return {"ok": True, "pushed": True, "status": read_status(vault_root)}


def commits_ahead_of_upstream(vault_root: Path) -> int:
    """Local commits not on ``@{u}`` (0 if there is no upstream)."""
    proc = _run_git(vault_root, "rev-list", "--count", "@{u}..HEAD")
    if proc.returncode != 0:
        return 0
    try:
        return int((proc.stdout or "0").strip() or 0)
    except ValueError:
        return 0


def push_if_ahead(vault_root: Path) -> dict[str, Any] | None:
    """Push when HEAD is ahead of upstream (e.g. a push lost to an outage).

    Returns the push result, or ``None`` when there was nothing to push or
    ``auto_push`` is off.
    """
    settings = sync_settings(vault_root)
    if not settings.auto_push or commits_ahead_of_upstream(vault_root) <= 0:
        return None
    return _push(vault_root, settings)


def pull_ff_only(vault_root: Path) -> dict[str, Any]:
    """``git pull --ff-only``. Network blip → offline; any other failure → block."""
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
    proc = _run_git_network(vault_root, "pull", "--ff-only")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "git pull --ff-only failed").strip()
        return _network_failure(vault_root, err, "pull_failed", st=st)

    st = read_status(vault_root)
    st["last_pull"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": True,
        "summary": (proc.stdout or "").strip()[:400],
    }
    st["state"] = "ok"
    st["error"] = None
    st["blocked_at"] = None
    _clear_offline(st)
    write_status(vault_root, st)
    return {"ok": True, "pulled": True, "status": read_status(vault_root), "branch": settings.default_branch}


def rebase_onto_remote(vault_root: Path) -> dict[str, Any]:
    """Fetch + rebase local commits onto ``origin/<default_branch>``.

    Recovers the case ``pull_ff_only`` cannot: local commits exist that the
    remote doesn't have (typically another session's sync won the race and
    pushed first). Never merges — replays local commits on top of the fetched
    remote tip so the eventual push stays a fast-forward (no ``--force``
    needed, same push path as everything else here).

    On conflict, aborts the rebase immediately so the working tree never
    carries ``<<<<<<<`` markers into a note the vault would otherwise index,
    then blocks with the conflict detail. This is a rollback, not a
    resolution — content conflicts still require a human/agent to look and
    re-apply, matching the "no auto-resolve" contract of the rest of this
    module.
    """
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

    fetch = _run_git_network(vault_root, "fetch", "origin", settings.default_branch)
    if fetch.returncode != 0:
        err = (fetch.stderr or fetch.stdout or "git fetch failed").strip()
        return _network_failure(vault_root, err, "fetch_failed", st=st)

    proc = _run_git(vault_root, "rebase", f"origin/{settings.default_branch}")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "git rebase failed").strip()
        abort = _run_git(vault_root, "rebase", "--abort")
        if abort.returncode != 0:
            abort_err = (abort.stderr or abort.stdout or "").strip()[:200]
            err = f"{err} (rebase --abort also failed: {abort_err}; working tree may still be mid-rebase)"
        _block(vault_root, f"rebase_conflict: {err}", st=st)
        return {"ok": False, "error": "rebase_conflict", "message": err[:500], "status": read_status(vault_root)}

    st = read_status(vault_root)
    st["last_pull"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": True,
        "summary": f"rebased onto origin/{settings.default_branch}: {(proc.stdout or '').strip()[:380]}",
    }
    st["state"] = "ok"
    st["error"] = None
    st["blocked_at"] = None
    _clear_offline(st)
    write_status(vault_root, st)

    result: dict[str, Any] = {
        "ok": True,
        "rebased": True,
        "pushed": False,
        "status": read_status(vault_root),
        "branch": settings.default_branch,
    }
    if settings.auto_push:
        push_result = _push(vault_root, settings)
        if not push_result["ok"]:
            return push_result
        result["pushed"] = True
        result["status"] = push_result["status"]
    return result


def run_action(
    vault_root: Path,
    action: Action,
    *,
    message: str | None = None,
) -> dict[str, Any]:
    """MCP/RPC entry: status | run | pull | rebase | clear_block."""
    with _lock_for(vault_root):
        if action == "status":
            st = read_status(vault_root)
            return {
                "ok": True,
                "action": "status",
                "sync_enabled": sync_enabled(vault_root),
                "contract_active": git_contract.git_contract_active(vault_root),
                "unsafe": unsafe_git_state(vault_root),
                "offline": st.get("state") == "offline",
                "next_retry_at": st.get("next_retry_at") if st.get("state") == "offline" else None,
                "retry_count": int(st.get("retry_count") or 0) if st.get("state") == "offline" else 0,
                "status": st,
            }
        if action == "clear_block":
            st = clear_block(vault_root)
            return {"ok": True, "action": "clear_block", "status": st}
        if not sync_enabled(vault_root) and action in ("run", "pull", "rebase"):
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
        if action == "rebase":
            return {**rebase_onto_remote(vault_root), "action": "rebase"}
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
        st = read_status(self.root)
        if st.get("state") == "blocked":
            if _heal_transient_block(self.root, st) is None:
                return
            if self.verbose:
                logger.info("git-sync: network-error block converted to offline; retrying")
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
                    logger.info(
                        "git-sync commit: %s %r",
                        result.get("hash", "")[:8],
                        result.get("message"),
                    )
                elif result.get("error") == "offline":
                    logger.info("git-sync offline (commit kept local): %s", result.get("message"))
                elif not result.get("ok"):
                    logger.warning("git-sync blocked: %s", result.get("message"))
            return

        # Idle pull: no pending commit debounce, index not busy
        with self._lock:
            commit_waiting = self._commit_due_at is not None
        if commit_waiting or index_busy:
            return
        from apo_engine import core as _core

        if _core.index_lock_backoff_active():
            if self.verbose:
                logger.info("git-sync pull: skipped (index_busy)")
            return
        if _core.is_wal_over_limit():
            if self.verbose:
                logger.info("git-sync pull: skipped (wal_over_limit)")
            return
        if offline_retry_due(read_status(self.root)):
            # Offline retry runs on its own backoff clock, not the pull interval.
            self._last_pull_at = now
            self._pull_then_push_backlog(label="offline retry")
            return
        if self._last_pull_at and (now - self._last_pull_at) < settings.pull_interval_seconds:
            return
        # First tick: don't pull immediately — wait one interval from start
        if self._last_pull_at == 0.0:
            self._last_pull_at = now
            return
        self._last_pull_at = now
        self._pull_then_push_backlog(label="pull")

    def _pull_then_push_backlog(self, *, label: str) -> None:
        """ff-only pull, then push any local commits a failed push left behind."""
        with _lock_for(self.root):
            result = pull_ff_only(self.root)
            if result.get("ok"):
                pushed = push_if_ahead(self.root)
                if pushed is not None:
                    result = pushed
        if not self.verbose:
            return
        if result.get("ok"):
            logger.info("git-sync %s: ok%s", label, " (pushed backlog)" if result.get("pushed") else "")
        elif result.get("error") == "offline":
            logger.info("git-sync %s: still offline: %s", label, result.get("message"))
        else:
            logger.warning("git-sync %s blocked: %s", label, result.get("message"))
