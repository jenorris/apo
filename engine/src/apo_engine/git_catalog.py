"""Git tree catalog projection for ``filter_notes(ref=)`` / ``read_note(ref=)``.

Indexes frontmatter/YAML only (no chunks, FTS, or embeddings) from a reachable
git commit at the **vault registry root**. Dirty worktrees and agent cwd are
out of scope — callers must pass an exported bookmark / branch tip.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from apo_engine import core
from apo_engine.note_format import is_note_path

_GIT_TIMEOUT_S = 60.0
_REF_CACHE_MAX = 8
# Bump when catalog parse / ignore semantics change (forces rebuild for same tree_oid).
_CATALOG_FORMAT_VERSION = "2"
_BUILD_COUNTER: dict[str, int] = {}  # tree_oid → build count (tests)
_MAX_CATALOG_NOTES = 5000
_MAX_BLOB_BYTES = 2 * 1024 * 1024
_MAX_CATALOG_BYTES = 64 * 1024 * 1024
# Raw ls-tree / log listing budget (paths only; before note filter).
_MAX_TREE_LIST_BYTES = 32 * 1024 * 1024
_MAX_LOG_LIST_BYTES = 64 * 1024 * 1024

# Inherited GIT_* can override ``git -C <vault>`` and redirect / reconfigure git.
_GIT_ENV_BLOCKLIST = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_EXEC_PATH",
    "GIT_ATTR_SOURCE",
)


class GitCatalogError(Exception):
    """Ref resolve / blob read failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResolvedRef:
    ref: str
    commit_oid: str
    tree_oid: str
    tip_mtime: float


def _scrubbed_git_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _GIT_ENV_BLOCKLIST:
        env.pop(key, None)
    # GIT_CONFIG_COUNT / GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n (dynamic inject).
    for key in list(env):
        if key == "GIT_CONFIG_COUNT" or key.startswith("GIT_CONFIG_KEY_") or key.startswith(
            "GIT_CONFIG_VALUE_"
        ):
            env.pop(key, None)
    return env


def _run_git(
    root: Path,
    *args: str,
    timeout: float = _GIT_TIMEOUT_S,
    stdin_data: bytes | str | None = None,
) -> subprocess.CompletedProcess[Any]:
    text = not isinstance(stdin_data, (bytes, bytearray))
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=text,
            timeout=timeout,
            check=False,
            input=stdin_data,
            env=_scrubbed_git_env(),
        )
    except subprocess.TimeoutExpired as e:
        raise GitCatalogError(
            "git_error",
            f"git {' '.join(args[:3])} timed out after {timeout:.0f}s",
        ) from e


def _validate_ref(ref: str) -> str:
    r = (ref or "").strip()
    if not r:
        raise GitCatalogError("bad_request", "ref= is empty")
    if r.startswith("-"):
        raise GitCatalogError("bad_request", f"ref= must not look like a git option: {r!r}")
    if "\x00" in r or "\n" in r:
        raise GitCatalogError("bad_request", "ref= contains illegal characters")
    return r


def _validate_rel_path(rel_path: str) -> str:
    rel = rel_path.replace("\\", "/").lstrip("/")
    if not rel or rel.startswith("-") or "\x00" in rel:
        raise GitCatalogError("bad_path", f"invalid path: {rel_path!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise GitCatalogError("bad_path", f"invalid path: {rel_path!r}")
    return rel


def resolve_tree(vault_root: Path, ref: str) -> ResolvedRef:
    """Resolve ``ref`` to commit + tree OIDs at ``vault_root``."""
    r = _validate_ref(ref)
    root = vault_root.expanduser().resolve()
    commit = _run_git(root, "rev-parse", "--verify", "--end-of-options", f"{r}^{{commit}}")
    if commit.returncode != 0:
        # Older git without --end-of-options: retry classic form (ref already rejects leading -).
        commit = _run_git(root, "rev-parse", "--verify", f"{r}^{{commit}}")
    if commit.returncode != 0:
        err = (commit.stderr or commit.stdout or "unknown ref").strip()
        raise GitCatalogError("not_found", _ref_not_found_message(root, r, err))
    commit_oid = (commit.stdout or "").strip()
    if not all(c in "0123456789abcdef" for c in commit_oid.lower()) or len(commit_oid) < 7:
        raise GitCatalogError("git_error", f"unexpected commit oid: {commit_oid!r}")
    tree = _run_git(root, "rev-parse", "--verify", f"{commit_oid}^{{tree}}")
    if tree.returncode != 0:
        err = (tree.stderr or tree.stdout or "no tree").strip()
        raise GitCatalogError("not_found", f"cannot resolve tree for {r!r}: {err[:200]}")
    tree_oid = (tree.stdout or "").strip()
    if not all(c in "0123456789abcdef" for c in tree_oid.lower()) or len(tree_oid) < 7:
        raise GitCatalogError("git_error", f"unexpected tree oid: {tree_oid!r}")
    tip = _run_git(root, "log", "-1", "--format=%ct", commit_oid)
    try:
        tip_mtime = float((tip.stdout or "").strip() or "0")
    except ValueError:
        tip_mtime = 0.0
    return ResolvedRef(ref=r, commit_oid=commit_oid, tree_oid=tree_oid, tip_mtime=tip_mtime)


_MAX_LISTED_REFS = 200
_HINT_REF_NAMES = 8


def _ref_not_found_message(root: Path, ref: str, git_err: str) -> str:
    """Unknown ``ref=`` — point at reachable heads (jj export habit)."""
    try:
        names = [row["name"] for row in list_refs(root, kind="heads")[:_HINT_REF_NAMES]]
    except GitCatalogError:
        names = []
    hint = (
        f"git ref not found: {ref!r} at vault root — not exported here "
        f"(jj: bookmark then colocated export). "
        f"List more via apo_admin(action=invoke, name=list_refs)."
    )
    if names:
        hint += f" Reachable heads: {', '.join(names)}."
    extra = (git_err or "").strip()
    if extra:
        hint += f" ({extra[:160]})"
    return hint


def list_refs(vault_root: Path, *, kind: str = "heads") -> list[dict[str, Any]]:
    """Reachable refs at ``vault_root`` via ``git for-each-ref``.

    ``kind``: ``heads`` (``refs/heads/*``, including jj bookmarks), ``tags``,
    or ``all``.
    """
    k = (kind or "heads").strip().lower()
    if k not in ("heads", "tags", "all"):
        raise GitCatalogError("bad_request", "kind must be heads|tags|all")
    root = vault_root.expanduser().resolve()
    patterns: list[str] = []
    if k in ("heads", "all"):
        patterns.append("refs/heads/")
    if k in ("tags", "all"):
        patterns.append("refs/tags/")
    proc = _run_git(
        root,
        "for-each-ref",
        "--format=%(refname:short)\t%(objectname)\t%(committerdate:unix)",
        "--sort=-committerdate",
        *patterns,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "for-each-ref failed").strip()
        raise GitCatalogError("git_error", f"git for-each-ref failed: {err[:300]}")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name, oid = parts[0].strip(), parts[1].strip()
        if not name or name in seen:
            continue
        if not all(c in "0123456789abcdef" for c in oid.lower()) or len(oid) < 7:
            continue
        try:
            tip_mtime = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
        except ValueError:
            tip_mtime = 0.0
        seen.add(name)
        out.append({"name": name, "ref": name, "commit": oid, "tip_mtime": tip_mtime})
        if len(out) >= _MAX_LISTED_REFS:
            break
    return out


def _ignore_patterns(vault_root: Path) -> list[str]:
    """Same floor as index walk, without requiring vaults.bind for APO_IGNORE."""
    from apo_engine.note_format import DEFAULT_YAML_IGNORE
    from apo_engine import config

    patterns = [".git/*", ".obsidian/*", "*.excalidraw.md", *DEFAULT_YAML_IGNORE]
    for ignore_file in (config.IGNORE_FILE, vault_root / ".indexignore"):
        try:
            if ignore_file.exists():
                for line in ignore_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        patterns.append(line)
        except OSError:
            continue
    return patterns


def catalog_version(vault_root: Path) -> str:
    """Format version + ignore fingerprint — cache miss when either changes."""
    pats = _ignore_patterns(vault_root)
    digest = hashlib.blake2b(
        "\n".join(pats).encode("utf-8"), digest_size=8
    ).hexdigest()
    return f"{_CATALOG_FORMAT_VERSION}:{digest}"


def _watchdog_kill(proc: subprocess.Popen[Any], deadline: float) -> None:
    """Kill ``proc`` once ``deadline`` passes (unblocks hung pipe I/O)."""
    while proc.poll() is None:
        if time.monotonic() >= deadline:
            try:
                proc.kill()
            except OSError:
                pass
            return
        time.sleep(0.05)


def _close_pipes(proc: subprocess.Popen[Any]) -> bytes:
    """Close stdout; read+close stderr; return stderr bytes."""
    err_b = b""
    if proc.stderr is not None:
        try:
            err_b = proc.stderr.read() or b""
        except OSError:
            err_b = b""
        try:
            proc.stderr.close()
        except OSError:
            pass
    if proc.stdout is not None:
        try:
            proc.stdout.close()
        except OSError:
            pass
    return err_b


def _iter_nul_stream(
    stream: Any,
    *,
    deadline: float,
    max_bytes: int,
    label: str,
) -> Any:
    """Yield NUL-separated fields from ``stream``; enforce byte budget + deadline."""
    buf = b""
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise GitCatalogError(
                "git_error",
                f"git {label} timed out after {_GIT_TIMEOUT_S:.0f}s",
            )
        chunk = stream.read(65536)
        if not chunk:
            if buf:
                yield buf.decode("utf-8", "replace")
            return
        total += len(chunk)
        if total > max_bytes:
            raise GitCatalogError(
                "git_error",
                f"git {label} listing exceeds {max_bytes} bytes",
            )
        buf += chunk
        while b"\0" in buf:
            field, buf = buf.split(b"\0", 1)
            yield field.decode("utf-8", "replace")


def list_note_paths(vault_root: Path, tree_oid: str) -> list[str]:
    """Note paths at ``tree_oid`` after ignore filters.

    Streams ``ls-tree -z`` so non-ASCII paths are not C-quoted and huge trees
    cannot OOM via ``capture_output``.
    """
    root = vault_root.expanduser().resolve()
    deadline = time.monotonic() + _GIT_TIMEOUT_S
    proc = subprocess.Popen(
        ["git", "-C", str(root), "ls-tree", "-r", "-z", "--name-only", tree_oid],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_scrubbed_git_env(),
    )
    threading.Thread(target=_watchdog_kill, args=(proc, deadline), daemon=True).start()
    ignore_res = core._compile_ignore(_ignore_patterns(root))
    out: list[str] = []
    err_b = b""
    try:
        assert proc.stdout is not None
        for rel in _iter_nul_stream(
            proc.stdout,
            deadline=deadline,
            max_bytes=_MAX_TREE_LIST_BYTES,
            label="ls-tree",
        ):
            rel = rel.strip().replace("\\", "/")
            if not rel or not is_note_path(rel):
                continue
            if core._is_ignored(rel, ignore_res):
                continue
            out.append(rel)
            if len(out) > _MAX_CATALOG_NOTES:
                raise GitCatalogError(
                    "git_error",
                    f"ref catalog too large: >{_MAX_CATALOG_NOTES} notes at tree",
                )
    except GitCatalogError:
        if proc.poll() is None:
            proc.kill()
        raise
    finally:
        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        err_b = _close_pipes(proc)

    if proc.returncode not in (0, None):
        err = (err_b or b"ls-tree failed").decode("utf-8", "replace").strip()
        raise GitCatalogError("git_error", f"git ls-tree failed: {err[:300]}")
    return out


def list_blob_oids(vault_root: Path, tree_oid: str, paths: list[str]) -> dict[str, str]:
    """Map path → blob oid for paths present in ``tree_oid`` (streamed ls-tree)."""
    if not paths:
        return {}
    root = vault_root.expanduser().resolve()
    want = set(paths)
    deadline = time.monotonic() + _GIT_TIMEOUT_S
    proc = subprocess.Popen(
        ["git", "-C", str(root), "ls-tree", "-r", "-z", tree_oid],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_scrubbed_git_env(),
    )
    threading.Thread(target=_watchdog_kill, args=(proc, deadline), daemon=True).start()
    out: dict[str, str] = {}
    err_b = b""
    early_stop = False
    try:
        assert proc.stdout is not None
        for entry in _iter_nul_stream(
            proc.stdout,
            deadline=deadline,
            max_bytes=_MAX_TREE_LIST_BYTES,
            label="ls-tree",
        ):
            if not entry:
                continue
            try:
                meta, path = entry.split("\t", 1)
            except ValueError:
                continue
            parts = meta.split()
            if len(parts) < 3 or parts[1] != "blob":
                continue
            rel = path.replace("\\", "/")
            if rel in want:
                out[rel] = parts[2]
                if len(out) >= len(want):
                    early_stop = True
                    break
    except GitCatalogError:
        if proc.poll() is None:
            proc.kill()
        raise
    finally:
        # Only SIGKILL for intentional early stop; otherwise wait for clean exit.
        if early_stop and proc.poll() is None:
            proc.kill()
        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        err_b = _close_pipes(proc)

    if early_stop:
        return out
    if proc.returncode not in (0, None):
        err = (err_b or b"ls-tree failed").decode("utf-8", "replace").strip()
        raise GitCatalogError(
            "git_error",
            f"git ls-tree failed (exit {proc.returncode}): {err[:300]}",
        )
    return out


def _read_exact(stream: Any, n: int, *, deadline: float) -> bytes:
    """Read exactly ``n`` bytes or raise on EOF / timeout."""
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        if time.monotonic() > deadline:
            raise GitCatalogError(
                "git_error",
                f"git cat-file --batch timed out after {_GIT_TIMEOUT_S:.0f}s",
            )
        chunk = stream.read(remaining)
        if not chunk:
            raise GitCatalogError("git_error", "git cat-file --batch ended early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _drain(stream: Any, n: int, *, deadline: float, chunk_size: int = 65536) -> None:
    """Discard ``n`` bytes from ``stream`` without retaining them."""
    remaining = n
    while remaining > 0:
        if time.monotonic() > deadline:
            raise GitCatalogError(
                "git_error",
                f"git cat-file --batch timed out after {_GIT_TIMEOUT_S:.0f}s",
            )
        chunk = stream.read(min(chunk_size, remaining))
        if not chunk:
            raise GitCatalogError("git_error", "git cat-file --batch ended early")
        remaining -= len(chunk)


def _readline_b(stream: Any, *, deadline: float, max_len: int = 4096) -> bytes:
    """Read one LF-terminated line (excluding LF), capped at ``max_len``."""
    if time.monotonic() > deadline:
        raise GitCatalogError(
            "git_error",
            f"git cat-file --batch timed out after {_GIT_TIMEOUT_S:.0f}s",
        )
    line = stream.readline(max_len + 2)
    if not line:
        return b""
    if not line.endswith(b"\n") and len(line) > max_len:
        raise GitCatalogError("git_error", "git cat-file --batch header too long")
    return line[:-1] if line.endswith(b"\n") else line


def read_blob(vault_root: Path, commit_oid: str, rel_path: str) -> str:
    """Read file text at ``commit_oid:rel_path`` (OID already resolved)."""
    root = vault_root.expanduser().resolve()
    rel = _validate_rel_path(rel_path)
    spec = f"{commit_oid}:{rel}"
    size_proc = _run_git(root, "cat-file", "-s", "--", spec)
    if size_proc.returncode != 0:
        err = (size_proc.stderr or size_proc.stdout or "not found").strip()
        raise GitCatalogError("not_found", f"blob not found: {rel!r} ({err[:200]})")
    try:
        size = int((size_proc.stdout or "0").strip())
    except ValueError as e:
        raise GitCatalogError("git_error", f"invalid blob size for {rel!r}") from e
    if size > _MAX_BLOB_BYTES:
        raise GitCatalogError(
            "git_error",
            f"blob too large: {rel!r} is {size} bytes (max {_MAX_BLOB_BYTES})",
        )
    proc = _run_git(root, "cat-file", "-p", "--", spec)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "not found").strip()
        raise GitCatalogError("not_found", f"blob not found: {rel!r} ({err[:200]})")
    raw = proc.stdout
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return raw if isinstance(raw, str) else ""


def _batch_read_blobs(vault_root: Path, path_to_oid: dict[str, str]) -> dict[str, str]:
    """Stream ``git cat-file --batch`` for many blob OIDs → path → text.

    Caps are enforced from headers **before** retaining blob bodies. Stdin is
    written on a side thread so a full OID list cannot deadlock against a full
    stdout pipe. A watchdog kills the process when the deadline elapses.
    """
    if not path_to_oid:
        return {}
    if len(path_to_oid) > _MAX_CATALOG_NOTES:
        raise GitCatalogError(
            "git_error",
            f"ref catalog too large: {len(path_to_oid)} notes (max {_MAX_CATALOG_NOTES})",
        )
    root = vault_root.expanduser().resolve()
    oid_to_paths: dict[str, list[str]] = {}
    for path, oid in path_to_oid.items():
        oid_to_paths.setdefault(oid, []).append(path)

    stdin = ("\n".join(oid_to_paths.keys()) + "\n").encode("utf-8")
    deadline = time.monotonic() + _GIT_TIMEOUT_S
    try:
        proc = subprocess.Popen(
            ["git", "-C", str(root), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_scrubbed_git_env(),
        )
    except OSError as e:
        raise GitCatalogError("git_error", f"git cat-file --batch failed to start: {e}") from e

    write_err: list[BaseException] = []
    expected = len(oid_to_paths)

    def _write_stdin() -> None:
        try:
            assert proc.stdin is not None
            view = memoryview(stdin)
            off = 0
            while off < len(view):
                if time.monotonic() > deadline:
                    write_err.append(TimeoutError("stdin write deadline"))
                    break
                n = proc.stdin.write(view[off : off + 65536])
                if not n:
                    write_err.append(BrokenPipeError("stdin write returned 0"))
                    break
                off += n
            else:
                # Completed full stdin without break.
                proc.stdin.close()
                return
            try:
                proc.stdin.close()
            except OSError:
                pass
        except BaseException as e:  # noqa: BLE001 — surface via write_err
            write_err.append(e)

    writer = threading.Thread(target=_write_stdin, daemon=True)
    writer.start()
    threading.Thread(target=_watchdog_kill, args=(proc, deadline), daemon=True).start()

    out: dict[str, str] = {}
    total = 0
    responses = 0
    err_b = b""
    try:
        assert proc.stdout is not None
        stdout = proc.stdout

        while True:
            if time.monotonic() > deadline:
                raise GitCatalogError(
                    "git_error",
                    f"git cat-file --batch timed out after {_GIT_TIMEOUT_S:.0f}s",
                )
            header_b = _readline_b(stdout, deadline=deadline)
            if not header_b:
                break
            header = header_b.decode("utf-8", "replace")
            parts = header.split()
            if len(parts) < 2:
                continue
            responses += 1
            if parts[1] == "missing":
                continue
            if len(parts) < 3:
                continue
            oid, _kind, size_s = parts[0], parts[1], parts[2]
            try:
                size = int(size_s)
            except ValueError:
                continue
            if size < 0:
                raise GitCatalogError("git_error", "git cat-file --batch reported negative size")

            body_and_nl = size + 1
            if size > _MAX_BLOB_BYTES:
                _drain(stdout, body_and_nl, deadline=deadline)
                continue
            if total + size > _MAX_CATALOG_BYTES:
                _drain(stdout, body_and_nl, deadline=deadline)
                raise GitCatalogError(
                    "git_error",
                    f"ref catalog exceeds {_MAX_CATALOG_BYTES} bytes compressed in-memory budget",
                )
            blob = _read_exact(stdout, size, deadline=deadline)
            _drain(stdout, 1, deadline=deadline)
            total += size
            text = blob.decode("utf-8", "replace")
            for path in oid_to_paths.get(oid, []):
                out[path] = text
    except GitCatalogError:
        if proc.poll() is None:
            proc.kill()
        raise
    finally:
        writer.join(timeout=5)
        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        err_b = _close_pipes(proc)

    if write_err:
        raise GitCatalogError(
            "git_error",
            f"git cat-file --batch stdin write failed: {write_err[0]!r}",
        )
    if responses < expected:
        raise GitCatalogError(
            "git_error",
            f"git cat-file --batch incomplete: {responses}/{expected} object responses",
        )
    # Any non-zero exit is a hard failure (do not return a silent partial catalog).
    if proc.returncode not in (0, None):
        err = (err_b or b"cat-file failed").decode("utf-8", "replace").strip()
        raise GitCatalogError(
            "git_error",
            f"git cat-file --batch failed (exit {proc.returncode}): {err[:300]}",
        )
    return out


def last_touch_mtimes(
    vault_root: Path,
    commit_oid: str,
    paths: list[str],
    *,
    tip_mtime: float,
) -> dict[str, float]:
    """Newest-first ``git log -z --name-only``; first sighting of each path wins.

    Streams log output (byte-capped). On timeout or git failure, unresolved paths
    keep ``tip_mtime`` (degraded sort).
    """
    if not paths:
        return {}
    root = vault_root.expanduser().resolve()
    remaining = set(paths)
    mtimes = {p: tip_mtime for p in paths}
    deadline = time.monotonic() + max(_GIT_TIMEOUT_S, 120.0)
    try:
        proc = subprocess.Popen(
            [
                "git",
                "-C",
                str(root),
                "log",
                "-z",
                "--format=%ct",
                "--name-only",
                commit_oid,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_scrubbed_git_env(),
        )
    except OSError:
        return mtimes

    threading.Thread(target=_watchdog_kill, args=(proc, deadline), daemon=True).start()
    try:
        assert proc.stdout is not None
        cur_ts: float | None = None
        for field in _iter_nul_stream(
            proc.stdout,
            deadline=deadline,
            max_bytes=_MAX_LOG_LIST_BYTES,
            label="log",
        ):
            if not remaining:
                break
            if not field:
                cur_ts = None
                continue
            if cur_ts is None:
                try:
                    cur_ts = float(field)
                except ValueError:
                    cur_ts = None
                continue
            rel = field.replace("\\", "/")
            if rel in remaining:
                mtimes[rel] = cur_ts
                remaining.discard(rel)
    except GitCatalogError:
        if proc.poll() is None:
            proc.kill()
        return mtimes
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.wait(timeout=5)
        _close_pipes(proc)
    return mtimes


def parse_catalog_row(rel: str, text: str) -> str | None:
    """JSON frontmatter for catalog, or None if not catalogable."""
    return core.note_catalog_json(text, rel)


def ensure_ref_tables(db: Any) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS ref_trees (
            tree_oid TEXT PRIMARY KEY,
            commit_oid TEXT NOT NULL,
            built_at REAL NOT NULL,
            catalog_version TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS ref_files (
            tree_oid TEXT NOT NULL,
            path TEXT NOT NULL,
            mtime REAL NOT NULL,
            frontmatter TEXT,
            PRIMARY KEY (tree_oid, path)
        );
        CREATE INDEX IF NOT EXISTS ref_files_tree ON ref_files(tree_oid);
        """
    )
    cols = {row[1] for row in db.execute("PRAGMA table_info(ref_trees)").fetchall()}
    if "catalog_version" not in cols:
        db.execute(
            "ALTER TABLE ref_trees ADD COLUMN catalog_version TEXT NOT NULL DEFAULT ''"
        )


def _evict_old_trees(db: Any, *, keep: int = _REF_CACHE_MAX) -> None:
    rows = db.execute(
        "SELECT tree_oid FROM ref_trees ORDER BY built_at DESC"
    ).fetchall()
    if len(rows) <= keep:
        return
    for (oid,) in rows[keep:]:
        db.execute("DELETE FROM ref_files WHERE tree_oid=?", (oid,))
        db.execute("DELETE FROM ref_trees WHERE tree_oid=?", (oid,))


def catalog_cached(db: Any, tree_oid: str, version: str) -> bool:
    row = db.execute(
        "SELECT catalog_version FROM ref_trees WHERE tree_oid=?",
        (tree_oid,),
    ).fetchone()
    if row is None:
        return False
    return str(row[0] or "") == version


def build_catalog(
    vault_root: Path,
    resolved: ResolvedRef,
    db: Any,
    *,
    force: bool = False,
) -> int:
    """Populate ``ref_files`` for ``resolved.tree_oid``. Returns row count.

    Skips rebuild when already cached for the current catalog version unless ``force``.
    """
    ensure_ref_tables(db)
    version = catalog_version(vault_root)
    if not force and catalog_cached(db, resolved.tree_oid, version):
        # Touch built_at so LRU prefers recently used trees.
        db.execute(
            "UPDATE ref_trees SET built_at=? WHERE tree_oid=?",
            (time.time(), resolved.tree_oid),
        )
        db.commit()
        n = db.execute(
            "SELECT COUNT(*) FROM ref_files WHERE tree_oid=?",
            (resolved.tree_oid,),
        ).fetchone()[0]
        return int(n)

    paths = list_note_paths(vault_root, resolved.tree_oid)
    path_oids = list_blob_oids(vault_root, resolved.tree_oid, paths)
    blobs = _batch_read_blobs(vault_root, path_oids)
    mtimes = last_touch_mtimes(
        vault_root, resolved.commit_oid, paths, tip_mtime=resolved.tip_mtime
    )

    try:
        db.execute("DELETE FROM ref_files WHERE tree_oid=?", (resolved.tree_oid,))
        db.execute("DELETE FROM ref_trees WHERE tree_oid=?", (resolved.tree_oid,))
        rows = 0
        for rel in paths:
            text = blobs.get(rel)
            if text is None:
                continue
            fm_json = parse_catalog_row(rel, text)
            if fm_json is None:
                continue
            db.execute(
                "INSERT INTO ref_files(tree_oid, path, mtime, frontmatter) VALUES (?,?,?,?)",
                (resolved.tree_oid, rel, mtimes.get(rel, resolved.tip_mtime), fm_json),
            )
            rows += 1
        db.execute(
            "INSERT INTO ref_trees(tree_oid, commit_oid, built_at, catalog_version) "
            "VALUES (?,?,?,?)",
            (resolved.tree_oid, resolved.commit_oid, time.time(), version),
        )
        _evict_old_trees(db)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    _BUILD_COUNTER[resolved.tree_oid] = _BUILD_COUNTER.get(resolved.tree_oid, 0) + 1
    return rows


def build_count(tree_oid: str) -> int:
    """Test helper: how many times ``build_catalog`` rebuilt this tree."""
    return int(_BUILD_COUNTER.get(tree_oid, 0))


def reset_build_counts() -> None:
    _BUILD_COUNTER.clear()


def ensure_catalog(
    vault_root: Path,
    ref: str,
    *,
    writer_connect: Callable[[], Any] | None = None,
) -> ResolvedRef:
    """Resolve ref and ensure catalog rows exist. Returns resolved tip."""
    resolved = resolve_tree(vault_root, ref)
    connect = writer_connect or core.writer_connect
    # writer_connect is process-local — do not close it.
    db = connect()
    build_catalog(vault_root, resolved, db)
    return resolved
