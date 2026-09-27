"""Git contract loader + file-level history.

Active when ``system/contracts/git-contract.schema.yaml`` (or legacy
``system/config/git-contract.schema.yaml``) exists under the vault root **and**
the vault is inside a git work tree (own ``.git`` or a parent checkout). Used by
``history`` for path mode and by ``git_sync`` when ``sync.enabled`` is set
(see ``apo_engine.git_sync``).
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from apo_engine import vault_contracts as vc

GIT_CONTRACT_CANDIDATES = (
    Path("system") / "contracts" / "git-contract.schema.yaml",
    Path("system") / "config" / "git-contract.schema.yaml",
)
GIT_CONTRACT_REL = GIT_CONTRACT_CANDIDATES[0]  # preferred path for docs/tests
_GIT_LOG_TIMEOUT_S = 15.0
HOSTS = ("github", "forgejo", "gitlab", "local")
KNOWN_TOP_KEYS = frozenset(
    {
        "git_contract_version",
        "remote",
        "host",
        "default_branch",
        "backup",
        "lfs",
        "never_commit",
        "sync",
        "restore",
    }
)
KNOWN_SYNC_KEYS = frozenset(
    {
        "enabled",
        "debounce_seconds",
        "pull_interval_seconds",
        "commit_message_template",
        "auto_push",
        "on_block_command",
    }
)
# Cache for the watcher's hot path (git-sync tick runs per vault per second).
_work_tree_cache_lock = threading.Lock()
_work_tree_cache: dict[str, tuple[float, bool]] = {}
# A vault root gaining/losing its git work tree is rare; re-probe occasionally
# rather than forking `git rev-parse` on every tick.
_WORK_TREE_TTL_S = float(os.environ.get("APO_GIT_WORKTREE_TTL_S") or 300.0)
# NUL-separated fields; RS separates commits
_LOG_FORMAT = "%H%x00%an%x00%aI%x00%s%x1e"


def resolve_git_contract_path(vault_root: Path, explicit: str | None = None) -> Path | None:
    if explicit is None:
        explicit = os.environ.get("APO_GIT_CONTRACT", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    for rel in GIT_CONTRACT_CANDIDATES:
        candidate = vault_root / rel
        if candidate.is_file():
            return candidate
    return None


def load_git_contract(vault_root: Path, explicit: str | None = None) -> dict[str, Any] | None:
    """Parse git-contract YAML if present. Returns None when missing/unreadable.

    Cached on (path, mtime_ns, size) via the shared contract cache: the
    watcher's git-sync tick reads this several times per vault per second, and
    re-parsing YAML each time was a measurable share of idle CPU. A contract
    edit changes mtime/size, so the cache self-invalidates.
    """
    path = resolve_git_contract_path(vault_root, explicit)
    if path is None:
        return None
    return vc.load_yaml_cached(path)


def check_contract(data: dict[str, Any]) -> list[dict[str, str]]:
    """Shape findings for a parsed git contract (advisory, never blocks)."""
    findings = vc.unknown_key_findings(data, KNOWN_TOP_KEYS)
    f = vc.enum_finding(data.get("host"), HOSTS, "host")
    if f:
        findings.append(f)
    findings.extend(vc.str_list_findings(data.get("never_commit"), "never_commit"))
    sync = data.get("sync")
    f = vc.mapping_finding(sync, "sync")
    if f:
        findings.append(f)
    elif isinstance(sync, dict):
        findings.extend(vc.unknown_key_findings(sync, KNOWN_SYNC_KEYS, prefix="sync"))
        for key in ("debounce_seconds", "pull_interval_seconds"):
            val = sync.get(key)
            if val is None:
                continue
            try:
                float(val)
            except (TypeError, ValueError):
                findings.append(
                    vc.finding(
                        "contract.invalid_value",
                        f"sync.{key}",
                        f"sync.{key} {val!r} is not a number (default used)",
                    )
                )
        if sync.get("enabled") and not str(data.get("remote") or "").strip():
            findings.append(
                vc.finding(
                    "contract.invalid_shape",
                    "remote",
                    "sync.enabled without remote: push/pull have nowhere to go",
                )
            )
    return findings


def is_git_work_tree(vault_root: Path) -> bool:
    """True if vault_root is inside a git work tree (root or subdirectory).

    Meta lives under ``~/Notes`` (``jenorris/foam``) without its own ``.git``;
    Norris/Work are dedicated checkouts with ``.git`` on the vault root.

    Result is TTL-cached: this forks a ``git`` subprocess, and the watcher's
    git-sync tick asked once per vault per second.
    """
    cache_key = str(vault_root)
    now = time.monotonic()
    with _work_tree_cache_lock:
        hit = _work_tree_cache.get(cache_key)
        if hit is not None and (now - hit[0]) < _WORK_TREE_TTL_S:
            return hit[1]
    try:
        proc = subprocess.run(
            ["git", "-C", str(vault_root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=_GIT_LOG_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Cache the negative too — a missing/hanging git must not be re-forked
        # every tick. TTL expiry retries.
        result = False
    else:
        result = proc.returncode == 0 and proc.stdout.strip() == "true"
    with _work_tree_cache_lock:
        if len(_work_tree_cache) > 64:
            _work_tree_cache.clear()
        _work_tree_cache[cache_key] = (now, result)
    return result


def git_contract_active(vault_root: Path) -> bool:
    """True when live git contract YAML exists and vault root is in a git work tree."""
    return load_git_contract(vault_root) is not None and is_git_work_tree(vault_root)


def git_file_log(
    vault_root: Path,
    rel_path: str,
    *,
    limit: int = 10,
) -> list[dict[str, str]]:
    """Return file-level commits via ``git log --follow`` (no blame/chunks).

    Raises RuntimeError on git failures (caller maps to tool error).
    """
    limit = max(1, min(int(limit), 100))
    rel = rel_path.replace("\\", "/").lstrip("/")
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(vault_root),
            "log",
            "--follow",
            f"-n{limit}",
            f"--format={_LOG_FORMAT}",
            "--",
            rel,
        ],
        capture_output=True,
        text=True,
        timeout=_GIT_LOG_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "git log failed").strip()
        raise RuntimeError(err[:500])
    commits: list[dict[str, str]] = []
    raw = proc.stdout or ""
    for block in raw.split("\x1e"):
        block = block.strip("\n\r")
        if not block:
            continue
        parts = block.split("\x00")
        if len(parts) < 4:
            continue
        commits.append(
            {
                "hash": parts[0],
                "author": parts[1],
                "date": parts[2],
                "subject": parts[3],
            }
        )
    return commits
