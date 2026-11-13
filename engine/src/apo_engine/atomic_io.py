"""Durable same-directory atomic file replacement."""

from __future__ import annotations

import errno
import os
import secrets
import stat
from pathlib import Path


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
