"""Multi-vault registry and per-request index binding.

True multi-index: each vault has its own NOTES_ROOT, INDEX_PATH, and deferred
COLLECTION. Active binding is a contextvar so core/search/watch use the right
sqlite without threading explicit paths through every call.

Config (optional) — ``APO_VAULTS`` path to JSON or inline JSON:

```json
{
  "default": "meta",
  "vaults": {
    "meta": {
      "root": "/Users/me/Notes/Meta",
      "index": "/Users/me/.apo/index-meta.db"
    },
    "work": {
      "root": "/Users/me/Notes/Work",
      "index": "/Users/me/.apo/index-work.db"
    }
  }
}
```

``collection`` (the deferred-queue namespace and telemetry partition key) is
not configurable — it's derived from the vault root by ``compute_vault_id``:
the short hash of the vault's git root commit when the root is a git repo
(stable across renames of the vault's ``vaults.json`` key or directory),
else a random id cached per-root in ``~/.apo/vault-ids.json``.

With no ``APO_VAULTS``, a single vault named ``default`` is built from
``APO_NOTES_ROOT`` / ``APO_INDEX`` / ``APO_COLLECTION`` (legacy single-vault).
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from apo_engine import config


@dataclass(frozen=True)
class VaultBinding:
    """Runtime binding for one vault's root + sqlite index + deferred namespace.

    ``read_only`` vaults are indexed and searchable but reject every write op.
    Set it via ``"read_only": true`` in the vault's ``APO_VAULTS`` entry; this
    is how an ingested foreign OKF bundle is mounted without letting an agent
    edit someone else's knowledge base.
    """

    name: str
    root: Path
    index: Path
    collection: str
    read_only: bool = False

    def resolved(self) -> VaultBinding:
        return VaultBinding(
            name=self.name,
            root=self.root.expanduser().resolve(),
            index=self.index.expanduser().resolve(),
            collection=self.collection,
            read_only=self.read_only,
        )


_vault_id_cache: dict[str, str] = {}
_FALLBACK_ID_STORE = Path.home() / ".apo" / "vault-ids.json"


def _git_root_id(root: Path) -> str | None:
    """Short hash of the vault's git root commit(s), or None if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-list", "--max-parents=0", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    # Sort for a deterministic pick when a repo has multiple root commits
    # (unrelated histories merged together).
    hashes = sorted(line.strip() for line in result.stdout.splitlines() if line.strip())
    return hashes[0][:12] if hashes else None


def _fallback_id(root: Path) -> str:
    """Random id for non-git vault roots, cached so it's stable across runs."""
    key = str(root)
    store = _FALLBACK_ID_STORE
    data: dict[str, str] = {}
    if store.exists():
        try:
            data = json.loads(store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    existing = data.get(key)
    if existing:
        return str(existing)
    new_id = secrets.token_hex(6)
    data[key] = new_id
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return new_id


def compute_vault_id(root: Path) -> str:
    """Stable per-vault collection id: git root-commit hash, else a cached random id.

    Memoized per resolved root for the life of the process — this runs on
    every ``load_bindings()`` call, so a subprocess spawn per lookup would
    add real per-request latency.
    """
    resolved = root.expanduser().resolve()
    key = str(resolved)
    cached = _vault_id_cache.get(key)
    if cached is not None:
        return cached
    vault_id = _git_root_id(resolved) or _fallback_id(resolved)
    _vault_id_cache[key] = vault_id
    return vault_id


_binding: ContextVar[VaultBinding | None] = ContextVar("apo_vault_binding", default=None)


def active() -> VaultBinding | None:
    return _binding.get()


def notes_root() -> Path:
    b = _binding.get()
    if b is not None:
        return b.root
    return Path(config.NOTES_ROOT).expanduser().resolve()


def index_path() -> Path:
    b = _binding.get()
    if b is not None:
        return b.index
    return Path(config.INDEX_PATH).expanduser().resolve()


def collection() -> str:
    b = _binding.get()
    if b is not None:
        return b.collection
    return str(config.COLLECTION)


@contextmanager
def bind(binding: VaultBinding) -> Iterator[VaultBinding]:
    """Activate a vault for the current context (MCP tool call / watch worker)."""
    resolved = binding.resolved()
    token = _binding.set(resolved)
    try:
        yield resolved
    finally:
        _binding.reset(token)


def _path(val: str | Path, default: Path | None = None) -> Path:
    if val is None or val == "":
        if default is None:
            raise ValueError("path required")
        return Path(default).expanduser().resolve()
    return Path(str(val)).expanduser().resolve()


def _load_vaults_raw() -> dict | None:
    raw = os.environ.get("APO_VAULTS", "").strip()
    if not raw:
        return None
    if raw.startswith("{"):
        data = json.loads(raw)
    else:
        p = Path(raw).expanduser()
        data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("APO_VAULTS must be a JSON object")
    return data


def load_bindings() -> tuple[str, dict[str, VaultBinding]]:
    """Return (default_name, {name: VaultBinding}).

    Always at least one vault (legacy single-root when APO_VAULTS unset).
    """
    data = _load_vaults_raw()
    if not data:
        name = "default"
        b = VaultBinding(
            name=name,
            root=Path(config.NOTES_ROOT),
            index=Path(config.INDEX_PATH),
            collection=str(config.COLLECTION),
        ).resolved()
        return name, {name: b}

    vaults_raw = data.get("vaults") or {}
    if not isinstance(vaults_raw, dict) or not vaults_raw:
        raise ValueError("APO_VAULTS.vaults must be a non-empty object")

    out: dict[str, VaultBinding] = {}
    for name, spec in vaults_raw.items():
        if not isinstance(spec, dict):
            raise ValueError(f"vault {name!r} spec must be an object")
        root = spec.get("root") or spec.get("notes_root")
        if not root:
            raise ValueError(f"vault {name!r} missing root")
        idx = spec.get("index") or spec.get("index_path")
        if not idx:
            # Sensible default under ~/.apo/
            idx = Path.home() / ".apo" / f"index-{name}.db"
        root_path = _path(root)
        out[str(name)] = VaultBinding(
            name=str(name),
            root=root_path,
            index=_path(idx),
            collection=compute_vault_id(root_path),
            read_only=bool(spec.get("read_only") or spec.get("readonly")),
        ).resolved()

    default = str(data.get("default") or next(iter(out)))
    if default not in out:
        raise ValueError(f"APO_VAULTS.default {default!r} not in vaults")
    return default, out


def binding_from_legacy_env(name: str = "default") -> VaultBinding:
    return VaultBinding(
        name=name,
        root=Path(config.NOTES_ROOT),
        index=Path(config.INDEX_PATH),
        collection=str(config.COLLECTION),
    ).resolved()
