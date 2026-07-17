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
      "index": "/Users/me/.apo/index-meta.db",
      "collection": "meta"
    },
    "work": {
      "root": "/Users/me/Notes/Work",
      "index": "/Users/me/.apo/index-work.db",
      "collection": "work"
    }
  }
}
```

With no ``APO_VAULTS``, a single vault named ``default`` is built from
``APO_NOTES_ROOT`` / ``APO_INDEX`` / ``APO_COLLECTION`` (legacy single-vault).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from apo_engine import config


@dataclass(frozen=True)
class VaultBinding:
    """Runtime binding for one vault's root + sqlite index + deferred namespace."""

    name: str
    root: Path
    index: Path
    collection: str

    def resolved(self) -> VaultBinding:
        return VaultBinding(
            name=self.name,
            root=self.root.expanduser().resolve(),
            index=self.index.expanduser().resolve(),
            collection=self.collection,
        )


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
        coll = spec.get("collection") or name
        out[str(name)] = VaultBinding(
            name=str(name),
            root=_path(root),
            index=_path(idx),
            collection=str(coll),
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
