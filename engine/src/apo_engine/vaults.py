"""Multi-vault registry and per-request index binding.

True multi-index: each vault has its own NOTES_ROOT, INDEX_PATH, and deferred
COLLECTION. Active binding is a contextvar so core/search/watch use the right
sqlite without threading explicit paths through every call.

Discovery (preferred → fallback):

1. ``APO_COLLECTION_ROOT`` / ``--collection-root`` — parent directory of vaults;
   each immediate child with a usage-contract ``vault_id`` is registered.
2. ``APO_VAULT_PATHS`` / repeatable ``--vault-path PATH`` — explicit roots
   (Workbench escape hatch for non-sibling trees such as ``compliance``).
3. ``APO_VAULTS`` JSON registry — **roots only**; object keys and any
   ``collection`` field are ignored (names come from usage ``vault_id``).
   Still the only way to mark a vault ``read_only`` — this is how
   ``apo-engine okf ingest`` mounts a foreign OKF bundle. Prefer
   ``APO_COLLECTION_ROOT`` / ``APO_VAULT_PATHS`` for ordinary (writable)
   multi-vault registration; emits a one-shot stderr note when used for that.
4. Legacy single-root: ``APO_NOTES_ROOT`` / ``APO_INDEX`` / ``APO_COLLECTION``.

Tool-facing name is always usage-contract ``vault_id``. Internal queue /
telemetry partition id is ``compute_collection_id(root)`` (git root-commit
hash or cached random) — never CLI-configurable. Default index path is
``~/.apo/index-{collection_id}.db``, with a legacy ``index-{vault_id}.db``
fallback when the collection-keyed file is absent (cutover).
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import yaml

from apo_engine import config

_APO_DISABLED = Path("system") / "contracts" / ".apo-disabled"
_USAGE_CANDIDATES = (
    Path("system") / "contracts" / "usage-contract.schema.yaml",
    Path("system") / "contracts" / "usage-contract.yaml",
    Path("system") / "config" / "usage-contract.schema.yaml",
    Path("system") / "config" / "usage-contract.yaml",
)

# One-shot stderr warn for APO_VAULTS shim per process.
_APO_VAULTS_WARNED = False


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


def compute_collection_id(root: Path) -> str:
    """Stable per-root queue/telemetry id: git root-commit hash, else cached random.

    Memoized per resolved root for the life of the process.
    """
    resolved = root.expanduser().resolve()
    key = str(resolved)
    cached = _vault_id_cache.get(key)
    if cached is not None:
        return cached
    collection_id = _git_root_id(resolved) or _fallback_id(resolved)
    _vault_id_cache[key] = collection_id
    return collection_id


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


def _read_usage_data(root: Path) -> dict[str, Any] | None:
    """Parse usage-contract YAML for a vault root, or None if missing/invalid."""
    for rel in _USAGE_CANDIDATES:
        path = root / rel
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            data = yaml.safe_load(raw)
        except (OSError, yaml.YAMLError):
            return None
        if isinstance(data, dict):
            return data
        return None
    return None


def read_usage_vault_id(root: Path) -> str | None:
    """Return non-empty usage-contract ``vault_id``, or None if not a vault."""
    if (root / _APO_DISABLED).exists():
        return None
    data = _read_usage_data(root)
    if not data:
        return None
    vid = str(data.get("vault_id") or "").strip()
    return vid or None


def read_usage_default_vault_claim(root: Path) -> str | None:
    """Return ``memory.default_vault`` from usage-contract when set."""
    data = _read_usage_data(root)
    if not data:
        return None
    memory = data.get("memory")
    if not isinstance(memory, dict):
        return None
    claim = str(memory.get("default_vault") or "").strip()
    return claim or None


_usage_layout_cache_lock = threading.Lock()
_usage_layout_cache: dict[str, tuple[float, dict[str, str]]] = {}


def read_usage_layout(root: Path) -> dict[str, str]:
    """Return usage-contract ``layout`` (top-level folder -> one-line description), or {}.

    Adapted from qmd (github.com/tobi/qmd)'s per-path search-result context, which it
    calls its key feature: results self-describe their neighborhood inline instead of
    requiring an agent to already hold the whole vault's folder layout in context.
    ``vault_project.format_layout_line`` already renders this same ``layout`` dict for
    the static desk projection — this is the per-search-hit sibling of that.

    Cached per resolved root, invalidated on the contract file's own mtime (one stat
    on a cache hit, no YAML re-parse) — this is called once per vault per search, and
    a search result count of 1 is the whole reason ``load_bindings()`` needed the same
    treatment.
    """
    resolved = root.expanduser().resolve()
    key = str(resolved)
    mtime = 0.0
    for rel in _USAGE_CANDIDATES:
        p = resolved / rel
        try:
            mtime = p.stat().st_mtime
            break
        except OSError:
            continue
    with _usage_layout_cache_lock:
        cached = _usage_layout_cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    data = _read_usage_data(resolved)
    layout = data.get("layout") if data else None
    out = (
        {str(k): str(v) for k, v in layout.items() if str(v or "").strip()}
        if isinstance(layout, dict)
        else {}
    )
    with _usage_layout_cache_lock:
        _usage_layout_cache[key] = (mtime, out)
    return out


def build_path_context(path: str, layout: dict[str, str]) -> list[dict[str, str]]:
    """Hierarchical path segments with layout labels (qmd-style context tree)."""
    norm = path.replace("\\", "/").strip("/")
    parts = norm.split("/")
    tree: list[dict[str, str]] = []
    for i, seg in enumerate(parts):
        if i == len(parts) - 1:
            label = Path(seg).stem.replace("-", " ").replace("_", " ").strip() or seg
        else:
            label = layout.get(seg, seg.replace("-", " "))
        tree.append({"segment": seg, "label": label})
    return tree


def _index_file_count(path: Path) -> int | None:
    """Return files-table row count, or None if unreadable / missing table."""
    try:
        import sqlite3

        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='files'"
            ).fetchone()
            if not row or not row[0]:
                return None
            return int(db.execute("SELECT count(*) FROM files").fetchone()[0])
    except Exception:
        return None


def _default_index_for(root: Path, vault_id: str) -> Path:
    """Prefer collection-id index; fall back to the legacy name-keyed file if present.

    Cutover rule: a brand-new empty ``index-{collection_id}.db`` never strands a
    vault that already has a populated legacy ``index-{vault_id}.db`` — that one
    wins until the collection-id file catches up.
    """
    coll = compute_collection_id(root)
    apo = Path.home() / ".apo"
    by_coll = apo / f"index-{coll}.db"
    by_name = apo / f"index-{vault_id}.db"

    if by_coll.exists():
        n = _index_file_count(by_coll)
        if n is not None and n > 0:
            return by_coll
        # Empty / unreadable collection-id file — fall through to the legacy pick.
    if by_name.exists():
        n = _index_file_count(by_name)
        if n is not None and n > 0:
            return by_name
    if by_name.exists() and not by_coll.exists():
        return by_name
    return by_coll


def _binding_for_root(root: Path) -> VaultBinding | None:
    """Build a binding from a vault root, or None if not a vault."""
    root_path = _path(root)
    if not root_path.is_dir():
        return None
    vault_id = read_usage_vault_id(root_path)
    if not vault_id:
        return None
    return VaultBinding(
        name=vault_id,
        root=root_path,
        index=_default_index_for(root_path, vault_id),
        collection=compute_collection_id(root_path),
    ).resolved()


def _warn_apo_vaults_shim() -> None:
    global _APO_VAULTS_WARNED
    if _APO_VAULTS_WARNED:
        return
    _APO_VAULTS_WARNED = True
    msg = (
        "APO_VAULTS: keys/collection are ignored; names come from usage-contract "
        "vault_id. Still supported for read-only vault mounts (apo-engine okf "
        "ingest) — for ordinary multi-vault registration prefer "
        "APO_COLLECTION_ROOT (parent directory of vaults) or APO_VAULT_PATHS / "
        "--vault-path."
    )
    print(f"apo: note: {msg}", file=sys.stderr)


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


def collection_root_path() -> Path | None:
    raw = (os.environ.get("APO_COLLECTION_ROOT") or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def explicit_vault_paths() -> list[Path]:
    """Paths from ``APO_VAULT_PATHS`` (colon-separated)."""
    out: list[Path] = []
    raw = (os.environ.get("APO_VAULT_PATHS") or "").strip()
    if raw:
        for part in raw.split(":"):
            part = part.strip()
            if part:
                out.append(Path(part).expanduser())
    return out


def discovery_active() -> bool:
    """True when path-list / collection-root / APO_VAULTS registry mode is on."""
    if collection_root_path() is not None:
        return True
    if explicit_vault_paths():
        return True
    raw = (os.environ.get("APO_VAULTS") or "").strip()
    return bool(raw)


def registry_source_path() -> Path | None:
    """Filesystem path whose mtime should wake the watcher, if any."""
    # Prefer APO_VAULTS file for compat; else collection root.
    raw = os.environ.get("APO_VAULTS", "").strip()
    if raw and not raw.startswith("{"):
        return Path(raw).expanduser()
    return collection_root_path()


def registry_mtime() -> float | None:
    """Max mtime of registry file, collection-root, or discovered children."""
    mtimes: list[float] = []
    src = registry_source_path()
    if src is not None:
        try:
            mtimes.append(src.stat().st_mtime)
        except OSError:
            pass
    root = collection_root_path()
    if root is not None and root.is_dir():
        try:
            mtimes.append(root.stat().st_mtime)
            for child in root.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    try:
                        mtimes.append(child.stat().st_mtime)
                    except OSError:
                        pass
        except OSError:
            pass
    return max(mtimes) if mtimes else None


def _add_binding(
    out: dict[str, VaultBinding],
    binding: VaultBinding,
    *,
    source: str,
) -> None:
    existing = out.get(binding.name)
    if existing is not None and str(existing.root) != str(binding.root):
        raise ValueError(
            f"duplicate vault_id {binding.name!r}: {existing.root} and {binding.root} "
            f"(via {source})"
        )
    out[binding.name] = binding


def _discover_collection_root(root: Path) -> dict[str, VaultBinding]:
    if not root.is_dir():
        raise ValueError(f"APO_COLLECTION_ROOT is not a directory: {root}")
    out: dict[str, VaultBinding] = {}
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        b = _binding_for_root(child)
        if b is None:
            continue  # Wiki / junk siblings without vault_id
        _add_binding(out, b, source=f"APO_COLLECTION_ROOT/{child.name}")
    return out


def _bindings_from_paths(paths: list[Path]) -> dict[str, VaultBinding]:
    out: dict[str, VaultBinding] = {}
    for p in paths:
        b = _binding_for_root(p)
        if b is None:
            raise ValueError(
                f"path is not a vault (missing usage-contract vault_id or .apo-disabled): {p}"
            )
        _add_binding(out, b, source=str(p))
    return out


def _bindings_from_apo_vaults_shim(data: dict) -> tuple[dict[str, VaultBinding], str | None]:
    """Compat: roots (+ optional index); JSON keys ignored. Returns (bindings, json_default)."""
    _warn_apo_vaults_shim()
    vaults_raw = data.get("vaults") or {}
    if not isinstance(vaults_raw, dict) or not vaults_raw:
        raise ValueError("APO_VAULTS.vaults must be a non-empty object")
    out: dict[str, VaultBinding] = {}
    for _key, spec in vaults_raw.items():
        if not isinstance(spec, dict):
            raise ValueError("APO_VAULTS vault spec must be an object")
        root = spec.get("root") or spec.get("notes_root")
        if not root:
            raise ValueError("APO_VAULTS vault entry missing root")
        root_path = _path(root)
        vault_id = read_usage_vault_id(root_path)
        if not vault_id:
            raise ValueError(
                f"path is not a vault (missing usage-contract vault_id or .apo-disabled): {root_path}"
            )
        idx = spec.get("index") or spec.get("index_path")
        index_path = _path(idx) if idx else _default_index_for(root_path, vault_id)
        b = VaultBinding(
            name=vault_id,
            root=root_path,
            index=index_path,
            collection=compute_collection_id(root_path),
            read_only=bool(spec.get("read_only") or spec.get("readonly")),
        ).resolved()
        _add_binding(out, b, source="APO_VAULTS")
    json_default = str(data.get("default") or "").strip() or None
    return out, json_default


def resolve_default_vault(
    bindings: dict[str, VaultBinding],
    *,
    explicit: str | None = None,
    json_default: str | None = None,
) -> str:
    """Resolve default vault name; fail if multi-vault and ambiguous.

    Order:
    1. ``explicit`` (``--default`` / ``APO_DEFAULT_VAULT``)
    2. Compat ``json_default`` from APO_VAULTS when it matches a loaded vault_id
    3. Exactly one vault
    4. Exactly one usage ``memory.default_vault`` claim among loaded vaults
    5. Fail
    """
    if not bindings:
        raise ValueError("no vaults loaded")

    if explicit:
        if explicit not in bindings:
            raise ValueError(
                f"default vault {explicit!r} not in registry; available: {sorted(bindings)}"
            )
        return explicit

    if json_default and json_default in bindings:
        return json_default

    if len(bindings) == 1:
        return next(iter(bindings))

    claims: list[str] = []
    for b in bindings.values():
        claim = read_usage_default_vault_claim(b.root)
        if not claim:
            continue
        if claim == b.name or claim in bindings:
            target = claim if claim in bindings else b.name
            if target not in claims:
                claims.append(target)
    if len(claims) == 1:
        return claims[0]

    raise ValueError(
        "ambiguous default vault — set APO_DEFAULT_VAULT / --default to one of: "
        f"{sorted(bindings)}"
    )


_bindings_cache_lock = threading.Lock()
_bindings_cache: dict[str, Any] | None = None  # {"fp": str, "at": float, "result": (...)}


def _bindings_fingerprint() -> str:
    """Cheap signature of the discovery *inputs* — not the (expensive) per-vault work.

    Changes immediately when the discovery env or the collection-root / registry-file
    mtime moves, so a real registry change invalidates the cache well before the TTL
    safety net in :func:`load_bindings` would.
    """
    return "\x1f".join(
        (
            os.environ.get("APO_COLLECTION_ROOT", ""),
            os.environ.get("APO_VAULT_PATHS", ""),
            os.environ.get("APO_VAULTS", ""),
            os.environ.get("APO_DEFAULT_VAULT", ""),
            os.environ.get("APO_NOTES_ROOT", ""),
            str(registry_mtime() or ""),
        )
    )


def invalidate_bindings_cache() -> None:
    """Drop the cached registry — next :func:`load_bindings` call rebuilds it."""
    global _bindings_cache
    with _bindings_cache_lock:
        _bindings_cache = None


def load_bindings(*, force: bool = False) -> tuple[str, dict[str, VaultBinding]]:
    """Return (default_name, {vault_id: VaultBinding}) — cached.

    Registry discovery (directory walk, one usage-contract YAML parse per vault, and
    index-file resolution) is real I/O, identical on nearly every call in a process
    that serves many MCP tool calls per session. Cached for
    ``config.BINDINGS_CACHE_TTL`` seconds; ``force=True`` (used by the
    ``apo_admin(reload_config)`` path) always bypasses it.
    """
    global _bindings_cache
    try:
        ttl = float(getattr(config, "BINDINGS_CACHE_TTL", 15.0))
    except (TypeError, ValueError):
        ttl = 15.0
    if not force and ttl > 0:
        fp = _bindings_fingerprint()
        with _bindings_cache_lock:
            cached = _bindings_cache
        if cached is not None and cached["fp"] == fp and (time.monotonic() - cached["at"]) < ttl:
            return cached["result"]
    else:
        fp = _bindings_fingerprint() if ttl > 0 else ""

    result = _load_bindings_uncached()
    if ttl > 0:
        with _bindings_cache_lock:
            _bindings_cache = {"fp": fp, "at": time.monotonic(), "result": result}
    return result


def _load_bindings_uncached() -> tuple[str, dict[str, VaultBinding]]:
    """Return (default_name, {vault_id: VaultBinding}) — always does the full discovery work."""
    out: dict[str, VaultBinding] = {}
    json_default: str | None = None

    coll_root = collection_root_path()
    if coll_root is not None:
        discovered = _discover_collection_root(_path(coll_root))
        for b in discovered.values():
            _add_binding(out, b, source="APO_COLLECTION_ROOT")

    paths = explicit_vault_paths()
    if paths:
        for b in _bindings_from_paths(paths).values():
            _add_binding(out, b, source="APO_VAULT_PATHS")

    data = None
    # Compat shim only when it is the sole discovery mechanism — do not merge a
    # leftover ~/.apo/vaults.json into COLLECTION_ROOT / VAULT_PATHS desks.
    if not out:
        data = _load_vaults_raw()
        if data is not None:
            shim, json_default = _bindings_from_apo_vaults_shim(data)
            for b in shim.values():
                _add_binding(out, b, source="APO_VAULTS")
    else:
        json_default = None

    if out:
        explicit = (os.environ.get("APO_DEFAULT_VAULT") or "").strip() or None
        default = resolve_default_vault(
            out, explicit=explicit, json_default=json_default
        )
        return default, out

    # Legacy single-root
    name = "default"
    b = VaultBinding(
        name=name,
        root=Path(config.NOTES_ROOT),
        index=Path(config.INDEX_PATH),
        collection=str(config.COLLECTION),
    ).resolved()
    return name, {name: b}


def binding_from_legacy_env(name: str = "default") -> VaultBinding:
    return VaultBinding(
        name=name,
        root=Path(config.NOTES_ROOT),
        index=Path(config.INDEX_PATH),
        collection=str(config.COLLECTION),
    ).resolved()


def apply_discovery_argv(argv: list[str] | None = None) -> list[str]:
    """Parse/strip discovery flags from argv; set env for :func:`load_bindings`.

    Flags:
      --vault-path PATH (repeatable; --vault is a back-compat alias — see note below)
      --default-vault NAME (--default is a back-compat alias)
      --collection-root DIR  (parent directory of vaults)

    Returns remaining argv (prog name preserved). Safe to call before
    ``load_bindings`` / MCP ``_load_vaults``.

    Note: this is the *only* place in the codebase where bare ``--vault`` means
    an explicit filesystem root. Everywhere else — ``apo-engine``/``apo-local``'s
    ``add_discovery_arguments`` (which already uses ``--vault-path`` for this),
    subcommand ``--vault``, ``okf_cli.py``, ``client/main.go`` — ``--vault`` means
    a registered ``vault_id`` *name*. ``--vault-path`` is the preferred spelling
    here for that reason; the bare ``--vault`` alias is kept only so an existing
    ``apo-mcp --vault <path>`` launch command doesn't break.
    """
    import argparse

    argv = list(sys.argv if argv is None else argv)
    if not argv:
        return argv
    prog, rest = argv[0], argv[1:]
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument(
        "--vault-path",
        "--vault",
        dest="vault_path",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "Explicit vault root (repeatable). Escape hatch for non-sibling roots. "
            "--vault is a back-compat alias — unlike apo-engine/apo-local's --vault "
            "NAME (a registered vault_id), this --vault takes a filesystem PATH."
        ),
    )
    p.add_argument(
        "--default-vault",
        "--default",
        dest="default_vault",
        default="",
        metavar="NAME",
        help=(
            "Default usage-contract vault_id when vault= is empty. --default is "
            "a back-compat alias (matches add_discovery_arguments' --default-vault)."
        ),
    )
    p.add_argument(
        "--collection-root",
        default="",
        metavar="DIR",
        help="Parent directory of vaults (autoconfigure). Not a queue/telemetry id.",
    )
    ns, remaining = p.parse_known_args(rest)

    if ns.collection_root:
        os.environ["APO_COLLECTION_ROOT"] = str(Path(ns.collection_root).expanduser())
    if ns.default_vault:
        os.environ["APO_DEFAULT_VAULT"] = str(ns.default_vault).strip()
    if ns.vault_path:
        # Merge with any existing APO_VAULT_PATHS
        existing = (os.environ.get("APO_VAULT_PATHS") or "").strip()
        parts = [p for p in existing.split(":") if p.strip()] if existing else []
        for v in ns.vault_path:
            parts.append(str(Path(v).expanduser()))
        # de-dupe preserving order
        seen: set[str] = set()
        uniq: list[str] = []
        for part in parts:
            key = str(Path(part).expanduser().resolve()) if Path(part).exists() else part
            if key in seen:
                continue
            seen.add(key)
            uniq.append(part)
        os.environ["APO_VAULT_PATHS"] = ":".join(uniq)

    return [prog, *remaining]


def add_discovery_arguments(parser: Any) -> None:
    """Attach shared discovery flags to an argparse parser (apo-engine CLI)."""
    parser.add_argument(
        "--vault-path",
        action="append",
        default=[],
        dest="vault_paths",
        metavar="PATH",
        help="Explicit vault root (repeatable). Sets APO_VAULT_PATHS. "
        "Not the same as subcommand --vault NAME.",
    )
    parser.add_argument(
        "--default-vault",
        default="",
        metavar="NAME",
        help="Default usage-contract vault_id (APO_DEFAULT_VAULT).",
    )
    parser.add_argument(
        "--collection-root",
        default="",
        metavar="DIR",
        help="Parent directory of vaults (APO_COLLECTION_ROOT). Not a collection id.",
    )


def apply_discovery_namespace(ns: Any) -> None:
    """Apply argparse namespace fields from :func:`add_discovery_arguments`."""
    coll = getattr(ns, "collection_root", "") or ""
    default = getattr(ns, "default_vault", "") or ""
    paths = getattr(ns, "vault_paths", None) or []
    if coll:
        os.environ["APO_COLLECTION_ROOT"] = str(Path(coll).expanduser())
    if default:
        os.environ["APO_DEFAULT_VAULT"] = str(default).strip()
    if paths:
        existing = (os.environ.get("APO_VAULT_PATHS") or "").strip()
        parts = [p for p in existing.split(":") if p.strip()] if existing else []
        parts.extend(str(Path(v).expanduser()) for v in paths)
        os.environ["APO_VAULT_PATHS"] = ":".join(parts)
