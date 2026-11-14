"""Issue, rescope, rotate and revoke token-auth clients (``mcp-tokens.json``).

The admin HTTP API (``/admin/clients`` in ``mcp/server.py``) is a thin layer
over :class:`TokenRegistry`. An admin is itself a client whose entry carries an
``admin`` policy, for example::

    "ops-admin": {"sha256": "...", "admin": {"prefix": "ops-", "vaults": ["atlas", "grid"]}}

and it may only manage clients whose names start with ``prefix``, and only
grant vaults in its ``vaults`` ceiling. It manages an existing client only
when that client's scope already sits inside the ceiling (never an unscoped
one), and it can never create or touch an admin entry, so a leaked admin
token can't escalate past its own policy. Plaintext
tokens are generated here, returned exactly once, and never written: the file
keeps only their SHA-256, like every other entry.

Writes take an ``fcntl`` lock on a sidecar ``<file>.lock`` and replace the file
atomically, keeping every other client entry as it was. The token verifier
reloads the file on its next request (mtime-keyed), so no restart is needed.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
PREFIX = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}-$")


class AdminError(Exception):
    """A refused admin request. ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class AdminPolicy:
    prefix: str
    vaults: frozenset[str]

    @classmethod
    def parse(cls, name: str, raw: Any) -> "AdminPolicy":
        if not isinstance(raw, dict):
            raise ValueError(f"client {name!r}: 'admin' must be an object")
        prefix = raw.get("prefix")
        vaults = raw.get("vaults")
        if not isinstance(prefix, str) or not PREFIX.match(prefix):
            raise ValueError(f"client {name!r}: admin.prefix must be a client-name prefix ending in '-', like 'ops-'")
        if not isinstance(vaults, list) or not vaults or not all(isinstance(v, str) and v.strip() for v in vaults):
            raise ValueError(f"client {name!r}: admin.vaults must be a non-empty list of vault ids")
        return cls(prefix=prefix, vaults=frozenset(v.strip() for v in vaults))


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public(name: str, entry: dict) -> dict:
    """An entry without its digest: what the API may show."""
    out = {"name": name}
    for key in ("vaults", "default_vault", "issued", "issued_by", "rotated"):
        if key in entry:
            out[key] = entry[key]
    return out


class TokenRegistry:
    def __init__(self, path: Path | str, known_vaults: Callable[[], Iterable[str]] | None = None):
        self.path = Path(path).expanduser()
        self._known_vaults = known_vaults

    # -- reads -------------------------------------------------------------

    def _load(self) -> dict:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("clients"), dict):
            raise AdminError("tokens file needs a top-level 'clients' object", 500)
        return raw

    def list(self, admin: AdminPolicy) -> list[dict]:
        clients = self._load()["clients"]
        return [
            _public(n, e)
            for n, e in sorted(clients.items())
            if n.startswith(admin.prefix) and _within_ceiling(admin, e)
        ]

    # -- writes ------------------------------------------------------------

    def issue(self, admin: AdminPolicy, actor: str, name: str, vaults: list[str], default_vault: str | None = None) -> dict:
        self._check_name(admin, name)
        vaults, default_vault = self._check_scope(admin, vaults, default_vault)
        token = secrets.token_hex(32)
        with self._editing() as data:
            if name in data["clients"]:
                raise AdminError(f"client {name!r} already exists; rotate or revoke it", 409)
            data["clients"][name] = {
                "sha256": _digest(token),
                "vaults": vaults,
                "default_vault": default_vault,
                "issued": _today(),
                "issued_by": actor,
            }
            entry = data["clients"][name]
        return _public(name, entry) | {"token": token}

    def rescope(self, admin: AdminPolicy, actor: str, name: str, vaults: list[str], default_vault: str | None = None) -> dict:
        self._check_name(admin, name)
        vaults, default_vault = self._check_scope(admin, vaults, default_vault)
        with self._editing() as data:
            entry = self._managed(admin, data, name)
            entry["vaults"], entry["default_vault"] = vaults, default_vault
            entry["issued_by"] = actor
        return _public(name, entry)

    def rotate(self, admin: AdminPolicy, actor: str, name: str) -> dict:
        self._check_name(admin, name)
        token = secrets.token_hex(32)
        with self._editing() as data:
            entry = self._managed(admin, data, name)
            entry["sha256"] = _digest(token)
            entry["rotated"] = _today()
            entry["issued_by"] = actor
        return _public(name, entry) | {"token": token}

    def revoke(self, admin: AdminPolicy, actor: str, name: str) -> None:
        self._check_name(admin, name)
        with self._editing() as data:
            self._managed(admin, data, name)
            del data["clients"][name]

    # -- checks ------------------------------------------------------------

    def _check_name(self, admin: AdminPolicy, name: str) -> None:
        if not isinstance(name, str) or not NAME.match(name):
            raise AdminError("client name must be lowercase letters, digits and dashes (2-64)")
        if not name.startswith(admin.prefix) or name == admin.prefix:
            raise AdminError(f"this admin may only manage clients named {admin.prefix}<something>", 403)

    def _check_scope(self, admin: AdminPolicy, vaults: Any, default_vault: Any) -> tuple[list[str], str]:
        if not isinstance(vaults, list) or not vaults or not all(isinstance(v, str) and v.strip() for v in vaults):
            raise AdminError("vaults must be a non-empty list of vault ids")
        vaults = list(dict.fromkeys(v.strip() for v in vaults))
        beyond = sorted(set(vaults) - admin.vaults)
        if beyond:
            raise AdminError(f"this admin may not grant: {', '.join(beyond)}", 403)
        if self._known_vaults is not None:
            unknown = sorted(set(vaults) - set(self._known_vaults()))
            if unknown:
                raise AdminError(f"unknown vault(s): {', '.join(unknown)}")
        default_vault = vaults[0] if default_vault is None else default_vault
        if not isinstance(default_vault, str) or default_vault.strip() not in vaults:
            raise AdminError("default_vault must be one of vaults")
        return vaults, default_vault.strip()

    @staticmethod
    def _managed(admin: AdminPolicy, data: dict, name: str) -> dict:
        """The entry for ``name`` if this admin may change it, else AdminError.

        The prefix is not enough: a hand-provisioned client that happens to
        match it may be unscoped (every vault) or scoped beyond the ceiling.
        Rotating such a client would hand the admin a token wider than its
        own policy, so only clients already inside the ceiling are managed.
        """
        entry = data["clients"].get(name)
        if entry is None:
            raise AdminError(f"no client {name!r}", 404)
        if not isinstance(entry, dict) or "admin" in entry:
            raise AdminError(f"client {name!r} is not manageable through this API", 403)
        if not _within_ceiling(admin, entry):
            raise AdminError(f"client {name!r} has a scope outside this admin's vaults", 403)
        return entry

    @contextmanager
    def _editing(self):
        lock_path = self.path.with_name(self.path.name + ".lock")
        with open(lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = self._load()
                yield data
                tmp = self.path.with_name(self.path.name + ".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _within_ceiling(admin: AdminPolicy, entry: Any) -> bool:
    """True for a non-admin entry scoped to a non-empty subset of the admin's vaults."""
    if not isinstance(entry, dict) or "admin" in entry:
        return False
    vaults = entry.get("vaults")
    if not isinstance(vaults, list) or not vaults:
        return False  # unscoped: every vault
    return all(isinstance(v, str) and v.strip() in admin.vaults for v in vaults)


def _today() -> str:
    return _dt.date.today().isoformat()
