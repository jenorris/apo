"""Durable same-directory atomic file replacement."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import os
import secrets
import stat
import threading
import unicodedata
from collections.abc import Iterator
from pathlib import Path

from apo_engine import deferred


_UNSUPPORTED_FSYNC_ERRNOS = {
    errno.EINVAL,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
}


def _create_temp(path: Path) -> tuple[int, Path]:
    """Create a same-directory temp with ordinary umask-masked file mode."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    for _ in range(100):
        candidate = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        try:
            return os.open(candidate, flags, 0o666), candidate
        except FileExistsError:
            continue
    raise FileExistsError(f"could not allocate temporary file for {path}")


def _fsync_parent_directory(parent: Path) -> None:
    """Persist the directory entry on POSIX filesystems that support it."""
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(parent, flags)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_FSYNC_ERRNOS or isinstance(exc, PermissionError):
            return
        raise
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_FSYNC_ERRNOS:
                raise
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes durably, then atomically replace ``path``."""
    path = Path(path)
    if path.is_symlink():
        raise OSError(errno.EPERM, f"refusing to replace symlink {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = None
    temp_path: Path | None = None
    try:
        fd, temp_path = _create_temp(path)
        with os.fdopen(fd, "wb") as temp:
            if mode is not None:
                os.fchmod(temp.fileno(), mode)
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_path, path)
        temp_path = None
        _fsync_parent_directory(path.parent)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Encode text and replace ``path`` with :func:`atomic_write_bytes`."""
    atomic_write_bytes(path, content.encode(encoding))


# --- note write lock -------------------------------------------------------
#
# A note write is stat -> read -> compute -> atomic replace. ``os.replace``
# swaps the inode, so the note file itself cannot carry a lock. The lock lives
# in a sidecar file outside the vault (``<runtime dir>/note-locks``): a lock
# file inside the vault would show up in git sync and the watcher.
#
# The path is hashed into ``_LOCK_STRIPES`` fixed lock files. This keeps the
# lock directory bounded, and the key is NFC + casefolded so two spellings of
# one file on a case/normalization-insensitive filesystem share a lock. Two
# different notes can share a stripe. That costs a little concurrency and never
# correctness.
_LOCK_STRIPES = 256
_held = threading.local()


def _stripe_for(path: Path) -> int:
    key = unicodedata.normalize("NFC", str(path)).casefold()
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % _LOCK_STRIPES


@contextlib.contextmanager
def note_write_lock(*paths: Path) -> Iterator[None]:
    """Hold an exclusive ``fcntl.flock`` for the canonical note ``paths``.

    Pass already-resolved paths. Take the lock before the stat that feeds an
    ``expected_mtime`` check and release it after the replace/unlink, so a
    concurrent writer sees the new mtime and returns ``stale_write``.

    Several paths (move) lock in sorted stripe order, so two movers cannot
    deadlock. ``flock`` conflicts between separate open file descriptions even
    inside one process, so threads exclude each other. A stripe this thread
    already holds is not locked again, because a second descriptor would
    deadlock against the first.
    """
    stripes = sorted({_stripe_for(Path(p)) for p in paths})
    held: dict[int, int] = getattr(_held, "stripes", None) or {}
    _held.stripes = held
    lock_dir = deferred.DEFERRED_DIR / "note-locks"
    opened: list[tuple[int, int]] = []
    try:
        for stripe in stripes:
            if held.get(stripe):
                held[stripe] += 1
                opened.append((stripe, -1))
                continue
            lock_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                lock_dir / f"{stripe:02x}.lock",
                os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except BaseException:
                os.close(fd)
                raise
            held[stripe] = 1
            opened.append((stripe, fd))
        yield
    finally:
        for stripe, fd in reversed(opened):
            held[stripe] -= 1
            if held[stripe] == 0:
                del held[stripe]
            if fd >= 0:
                os.close(fd)  # closing the descriptor drops the flock
