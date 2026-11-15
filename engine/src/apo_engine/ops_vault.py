"""Ops vault surface: vault_op (list/contracts/describe/merge/project/stats/lint)
and its lint sweep cache."""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Any

from apo_engine import (
    archival_contract,
    config,
    host_projection,
    note_lint,
    vault_contracts,
    vault_desk,
    vault_project,
    vaults,
)
from apo_engine.atomic_io import atomic_write_bytes
from apo_engine.ops_common import (
    _binding,
    _err,
    _load_bindings,
    _top_level_dirs,
    OpsError,
)
from apo_engine.ops_guard import _enqueue_index
from apo_engine.ops_read import graph_export


_lint_sweep_cache: dict[tuple[str, str, bool], tuple[float, dict[str, Any]]] = {}
_lint_sweep_cache_lock = threading.Lock()
_LINT_SWEEP_CACHE_TTL = 120.0


def _lint_cache_fingerprint(root: Path) -> str:
    # Every contract on disk: lint now depends on okf/read/archival/usage bodies
    # and reports contract-shape drift, so any contract edit must miss the cache.
    return vault_contracts.contract_fingerprint(root)


def _vault_lint_merged_payload(
    b: vaults.VaultBinding,
    *,
    folder_s: str,
    fix: bool,
    known_skills: list[str] | None,
) -> dict[str, Any]:
    """Full merged lint payload — cached across paginated vault(lint) calls."""
    root = b.resolved().root
    cache_key = (b.name, folder_s, bool(fix), _lint_cache_fingerprint(root))
    now = time.monotonic()
    if not fix:
        with _lint_sweep_cache_lock:
            hit = _lint_sweep_cache.get(cache_key)
            if hit is not None and now - hit[0] < _LINT_SWEEP_CACHE_TTL:
                return hit[1]
    data = archival_contract.load_archival_contract(root)
    if data is not None:
        data = archival_contract.parse_contract(data).normalized
    arch = archival_contract.lint_vault(
        root,
        data,
        folder=folder_s,
        limit=100_000,
        offset=0,
        vault_name=b.name,
    )
    notes = note_lint.lint_folder(
        root,
        folder=folder_s,
        limit=100_000,
        offset=0,
        vault_name=b.name,
        include_links=True,
        known_skills=known_skills,
        fix=bool(fix),
    )
    merged_flaws: list[dict[str, Any]] = []
    for part in (arch, notes):
        for f in part.get("flaws") or []:
            if isinstance(f, dict):
                merged_flaws.append(f)
    merged_flaws.extend(
        f.as_dict()
        for f in note_lint.detect_read_contract_type_mismatches(root, vault=b.name)
    )
    if not folder_s:
        # Contract-shape drift is vault-level; a folder-scoped sweep stays scoped.
        merged_flaws.extend(
            f.as_dict() for f in note_lint.contract_flaws(root, vault=b.name)
        )
    counts: dict[str, int] = {}
    for f in merged_flaws:
        code = str(f.get("code") or "?")
        counts[code] = counts.get(code, 0) + 1
    payload = {
        "merged_flaws": merged_flaws,
        "counts_by_code": counts,
        "arch": arch,
        "notes": notes,
    }
    if not fix:
        with _lint_sweep_cache_lock:
            _lint_sweep_cache[cache_key] = (now, payload)
    return payload


def _vault_row(b: vaults.VaultBinding, *, default_name: str) -> dict[str, Any]:
    root = b.resolved().root
    return {
        "root": str(root),
        "collection": b.collection,
        "ingest_dir": config.INGEST_DIR,
        "default": b.name == default_name,
        "top_level_dirs": _top_level_dirs(root),
        "contracts": vault_contracts.contracts_summary(root),
    }


def vault_op(
    action: str = "list",
    *,
    vault: str = "",
    vaults: list[str] | None = None,
    full: bool = False,
    days: int | None = 7,
    folder: str = "",
    limit: int | None = 50,
    offset: int = 0,
    fix: bool = False,
    known_skills: list[str] | None = None,
    contract: str = "",
    to: str = "",
    dry_run: bool = False,
    mode: str = "full",
    path: str = "",
    depth: int = 1,
    kinds: list[str] | None = None,
    include_unresolved: bool = False,
    max_nodes: int | None = 500,
    op: str = "",
    confirm: bool = False,
) -> dict[str, Any]:
    """Vault management: list | contracts | describe | merge | project | stats | lint | okf_dry_run | clone | export | grammar.

    Read-only except ``stats`` (habit KPI rollups) and ``clone`` (file copy). ``project`` returns
    desk ``body`` + ``guidance``; ``mode="full"`` (default) is the complete body, ``mode="index"``
    is a compact always-loaded pointer surface that tells the agent to call ``project`` again
    scoped to one vault (``vaults=[<id>]``) for that vault's full detail — see
    ``vault_project.render_desk_index`` / ``render_desk_body``. ``lint`` emits corpus ``flaws[]`` (archival + note_lint detectors)
    for one vault. ``fix=true`` on lint applies mechanical auto remediations (trailing WS) only.
    ``okf_dry_run`` (vault=, contract=<proposed okf-contract.schema.yaml text>) simulates
    that contract against the current corpus and reports what it would reclassify or
    newly flag — never writes anything, never touches the live contract on disk.
    ``clone`` (vault=<from>, to=<to>) copies every file under ``system/`` (contracts, config,
    schemas — not vault content) from an already-registered vault into another already-registered
    vault, for scaffolding a new persona/template vault from an existing one. ``dry_run=true``
    previews the file list without writing. Existing destination files are always skipped (never
    overwritten) — remove/rename first to replace one. Does not create or register vaults — both
    ``vault=`` and ``to=`` must already exist in the discovery registry.
    ``export`` returns the typed link graph as node-link JSON (whole vault, ``folder=``, or
    ``path=``/``depth=`` ego graph; ``kinds=``, ``include_unresolved=``; ``max_nodes`` cap).
    ``grammar`` (vault=, op=<write|query>) compiles a GBNF grammar for constraining an external
    local model's output to this vault's own schema — a closed-vocabulary ``folder-value``/
    ``okf-type-value`` templated from this vault's own contracts (usage-contract ``layout``,
    okf-contract ``path_rules``), so a grammar-constrained caller can never reference a folder or
    okf_type this vault doesn't declare. Read-only, never touches the vault; compiles one already-
    named vault's grammar at a time (no cross-vault vocabulary — mixing vaults would cross-pollinate
    contracts). ``op="write"`` covers write/append/patch envelopes; ``op="query"`` covers
    ``filter_notes``-style ``where=`` clauses. Response includes ``grammar_hash`` (blake2b-16 of the
    grammar text) so a caller can cache the compiled grammar and skip recompiling when it hasn't
    changed. Does not enumerate tags (no authoritative controlled tag vocabulary exists in Apo) or
    free-text fields (title/path/field values) — those stay shape-checked only in the grammar, never
    membership-checked.
    """
    act = (action or "list").strip().lower()
    if act not in (
        "list",
        "contracts",
        "describe",
        "merge",
        "project",
        "stats",
        "lint",
        "okf_dry_run",
        "clone",
        "export",
        "grammar",
    ):
        return _err(
            error="bad_action",
            message="action must be list|contracts|describe|merge|project|stats|lint|okf_dry_run|clone|export|grammar",
        )

    if act == "export":
        # Typed link graph as node-link JSON — see graph_export.
        return graph_export(
            vault=vault,
            folder=folder,
            path=path,
            depth=depth,
            kinds=kinds,
            include_unresolved=include_unresolved,
            limit=max_nodes,
        )

    if act == "okf_dry_run":
        if vaults:
            return _err(
                error="bad_request",
                message="okf_dry_run accepts vault= (single vault), not vaults=",
            )
        key = (vault or "").strip()
        try:
            b = _binding(key) if key else _binding("")
        except OpsError as e:
            return _err(error=e.code, message=e.message)
        if not (contract or "").strip():
            return _err(error="bad_request", message="okf_dry_run requires contract=<proposed YAML text>")
        from apo_engine import okf as okf_mod

        return okf_mod.okf_dry_run(b.resolved().root, b.name, contract)

    if act == "stats":
        from apo_engine import telemetry_ops

        key = (vault or "").strip()
        try:
            b = _binding(key) if key else _binding("")
        except OpsError as e:
            return _err(error=e.code, message=e.message)
        if days is not None and days < 0:
            return _err(error="bad_request", message="days must be >= 0 or null")
        out = telemetry_ops.vault_stats(
            vault_root=b.resolved().root,
            collection=b.collection,
            days=days,
        )
        out["vault"] = b.name
        return out

    if act == "lint":
        if vaults:
            return _err(
                error="bad_request",
                message="lint accepts vault= (single vault), not vaults=",
            )
        key = (vault or "").strip()
        try:
            b = _binding(key) if key else _binding("")
        except OpsError as e:
            return _err(error=e.code, message=e.message)
        if limit is not None and limit < 0:
            return _err(error="bad_request", message="limit must be >= 0 or null")
        if offset < 0:
            return _err(error="bad_request", message="offset must be >= 0")
        folder_s = (folder or "").strip()
        lim = 50 if limit is None else int(limit)
        off = int(offset)
        merged = _vault_lint_merged_payload(
            b,
            folder_s=folder_s,
            fix=bool(fix),
            known_skills=known_skills,
        )
        merged_flaws = merged["merged_flaws"]
        counts = merged["counts_by_code"]
        arch = merged["arch"]
        notes = merged["notes"]
        sliced = merged_flaws[off : off + lim]
        out: dict[str, Any] = {
            "ok": True,
            "action": "lint",
            "flaws": sliced,
            "counts_by_code": counts,
            "total_flaws": len(merged_flaws),
            "has_more": (off + len(sliced)) < len(merged_flaws),
            "offset": off,
            "limit": lim,
            "vault": b.name,
            "folder": folder_s,
            "fix": bool(fix),
        }
        warnings: list[str] = []
        tips: list[str] = []
        for part in (arch, notes):
            w = part.get("warning")
            if w:
                warnings.append(str(w))
            t = part.get("tip")
            if t:
                tips.append(str(t))
        if warnings:
            out["warning"] = "; ".join(warnings)
        if tips:
            out["tip"] = "; ".join(dict.fromkeys(tips))
        return out

    if act == "grammar":
        if vaults:
            return _err(
                error="bad_request",
                message="grammar accepts vault= (single vault), not vaults=",
            )
        op_s = (op or "").strip().lower()
        if op_s not in ("write", "query"):
            return _err(error="bad_request", message="grammar requires op=write|query")
        key = (vault or "").strip()
        try:
            b = _binding(key) if key else _binding("")
        except OpsError as e:
            return _err(error=e.code, message=e.message)
        root = b.resolved().root
        found = vault_contracts.discover_contracts(root)
        contracts = vault_contracts.present_contracts(found, full=True)
        row = {"contracts": contracts}
        compiled = host_projection.compile_vault_grammar(
            row, op_s, top_level_dirs=_top_level_dirs(root)
        )
        grammar_text = compiled["grammar"]
        grammar_hash = hashlib.blake2b(
            grammar_text.encode("utf-8", "replace"), digest_size=8
        ).hexdigest()
        return {
            "ok": True,
            "action": "grammar",
            "op": op_s,
            "vault": b.name,
            "grammar": grammar_text,
            "grammar_hash": grammar_hash,
            "folders": compiled["folders"],
            "okf_types": compiled["okf_types"],
        }

    if act == "clone":
        if vaults:
            return _err(
                error="bad_request",
                message="clone accepts vault= (single source), not vaults=",
            )
        to_key = (to or "").strip()
        if not to_key:
            return _err(error="bad_request", message="clone requires to=<destination vault id>")
        key = (vault or "").strip()
        try:
            src_b = _binding(key) if key else _binding("")
            dst_b = _binding(to_key, write=True)
        except OpsError as e:
            return _err(error=e.code, message=e.message)
        if src_b.name == dst_b.name:
            return _err(error="bad_request", message="clone requires two different vaults")
        if not dry_run and not confirm:
            return _err(
                error="confirm_required",
                message="clone copies system/ control files; pass confirm=true",
            )
        src_root = src_b.resolved().root
        dst_root = dst_b.resolved().root
        src_system = src_root / "system"
        if not src_system.is_dir():
            return _err(
                error="not_found",
                message=f"source vault {src_b.name!r} has no system/ directory",
            )
        copied: list[str] = []
        skipped: list[str] = []
        for f in sorted(src_system.rglob("*")):
            if f.is_symlink() or not f.is_file():
                continue
            try:
                if not f.resolve().is_relative_to(src_root.resolve()):
                    continue
            except OSError:
                continue
            rel = f.relative_to(src_root).as_posix()
            dst_file = (dst_root / rel)
            if dst_file.is_symlink():
                skipped.append(rel)
                continue
            try:
                if not dst_file.parent.resolve().is_relative_to(dst_root.resolve()):
                    skipped.append(rel)
                    continue
            except OSError:
                skipped.append(rel)
                continue
            if dst_file.exists():
                skipped.append(rel)
                continue
            copied.append(rel)
            if not dry_run:
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(dst_file, f.read_bytes())
        if not dry_run:
            for rel in copied:
                _enqueue_index(dst_b, dst_root / rel)
        return {
            "ok": True,
            "action": "clone",
            "from": src_b.name,
            "to": dst_b.name,
            "dry_run": bool(dry_run),
            "copied": copied,
            "skipped_existing": skipped,
        }

    try:
        default_name, bindings = _load_bindings()
    except Exception as e:
        return _err(error="bad_vault", message=str(e))

    if vaults:
        if vault:
            return _err(error="bad_request", message="pass vault= or vaults=, not both")
        names = [v.strip() for v in vaults if isinstance(v, str) and v.strip()]
        unknown = [n for n in names if n not in bindings]
        if unknown:
            return _err(
                error="bad_vault",
                message=f"unknown vault(s) {unknown!r}; available: {sorted(bindings)}",
            )
        bindings = {k: b for k, b in bindings.items() if k in names}
        if default_name not in bindings:
            default_name = sorted(bindings)[0] if bindings else default_name

    want_full = bool(full)

    if act == "list":
        return {
            "ok": True,
            "action": "list",
            "default_vault": default_name,
            "vaults": {
                name: _vault_row(b, default_name=default_name)
                for name, b in sorted(bindings.items())
            },
        }

    if act == "contracts":
        key = (vault or "").strip()
        if not key:
            out_vaults: dict[str, Any] = {}
            for name, b in sorted(bindings.items()):
                root = b.resolved().root
                found = vault_contracts.discover_contracts(root)
                out_vaults[name] = {
                    "root": str(root),
                    "contracts": vault_contracts.present_contracts(
                        found, full=want_full
                    ),
                }
            return {
                "ok": True,
                "action": "contracts",
                "full": want_full,
                "default_vault": default_name,
                "vaults": out_vaults,
            }
        try:
            b = _binding(key)
        except OpsError as e:
            return _err(error=e.code, message=e.message)
        root = b.resolved().root
        found = vault_contracts.discover_contracts(root)
        return {
            "ok": True,
            "action": "contracts",
            "full": want_full,
            "vault": b.name,
            "root": str(root),
            "contracts": vault_contracts.present_contracts(found, full=want_full),
        }

    def _merge_payload(*, bodies: bool) -> dict[str, Any]:
        desk = vault_desk.load_desk()
        roles = desk.get("vault_roles") if isinstance(desk.get("vault_roles"), dict) else {}
        merged_vaults: dict[str, Any] = {}
        for name, b in sorted(bindings.items()):
            root = b.resolved().root
            found = vault_contracts.discover_contracts(root)
            row: dict[str, Any] = {
                "root": str(root),
                "collection": b.collection,
                "ingest_dir": config.INGEST_DIR,
                "default": name == default_name,
                "read_only": b.read_only,
                "top_level_dirs": _top_level_dirs(root),
                "contract_ids": vault_contracts.contract_ids(root),
                "contracts": vault_contracts.present_contracts(found, full=bodies),
            }
            if name in roles:
                row["role"] = roles[name]
            merged_vaults[name] = row
        desk_meta = {
            "source": desk.get("_source"),
            "path": desk.get("_path"),
        }
        if desk.get("_error"):
            desk_meta["error"] = desk["_error"]
        pub_desk = vault_project.scope_desk_overlay(
            vault_desk.public_desk(desk), merged_vaults
        )
        return {
            "ok": True,
            "action": "merge",
            "full": bodies,
            "default_vault": default_name,
            "desk": pub_desk,
            "desk_meta": desk_meta,
            "merge_rules": {
                "cross_pollinate_contracts": bool(
                    desk.get("cross_pollinate_contracts", False)
                ),
                "desk_overlay_keys": [
                    "dual_write",
                    "citations",
                    "vault_roles",
                    "habits",
                    "pointers",
                    "role_notes",
                    "workspace",
                ],
            },
            "vaults": merged_vaults,
        }

    if act == "merge":
        return _merge_payload(bodies=want_full)

    if act == "project":
        # Full parsed bodies for every contract — render_desk_body() decides what to surface.
        merge = _merge_payload(bodies=True)
        if not merge.get("ok"):
            return merge
        proj_mode = (mode or "full").strip().lower()
        if proj_mode not in ("full", "index"):
            return _err(error="bad_request", message="mode must be full|index")
        # Provenance rides on the same inputs maybe_reproject watches, so a placed
        # file's `sources` hash can be diffed against a fresh render.
        merge["provenance"] = vault_project.provenance(
            desk_mtime=vault_project._desk_mtime(),
            contracts_sig=vault_project._contracts_signature(),
        )
        projected = host_projection.project(merge, mode=proj_mode)
        if not projected.get("ok"):
            return projected
        projected["default_vault"] = merge.get("default_vault")
        projected["desk_meta"] = merge.get("desk_meta")
        return projected

    # describe — single vault (default when vault empty). Resolve against the
    # (possibly vaults=-filtered) bindings loaded above, not a fresh _binding()
    # call, so a subset filter actually constrains which vault "default" means.
    key = (vault or "").strip() or default_name
    if key not in bindings:
        return _err(
            error="bad_vault",
            message=f"unknown vault {key!r}; available: {sorted(bindings)}",
        )
    b = bindings[key]
    root = b.resolved().root
    found = vault_contracts.discover_contracts(root)
    return {
        "ok": True,
        "action": "describe",
        "full": want_full,
        "vault": b.name,
        "root": str(root),
        "collection": b.collection,
        "ingest_dir": config.INGEST_DIR,
        "default": b.name == default_name,
        "top_level_dirs": _top_level_dirs(root),
        "contract_ids": list(found),
        "contracts": vault_contracts.present_contracts(found, full=want_full),
    }
