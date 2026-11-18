"""Shared vault operations — MCP and the CLIs (apo-engine/apo-local) return the
same {ok,…} shapes.

Read + write paths for gateways. Index writes stay watcher-owned (deferred enqueue).

This module is the stable import surface and holds no logic. The code lives in
role modules, and every old name is re-exported here so ``ops.write_note``,
``from apo_engine import ops`` and test patches on ``ops.<name>`` keep working:

- ``ops_common``  OpsError, vault binding, path resolution, tip helpers
- ``ops_admin``   watcher/index status, reindex, stats, index health, git sync
- ``ops_guard``   mtime CAS checks, touch tracking, index enqueue, write finalization
- ``ops_search``  search fan-out, filter_notes
- ``ops_read``    read_note, expand_chunk, backlinks, graph, history
- ``ops_mutate``  write_note, append_note, patch_note/patch_entry/patch_notes, table ops
- ``ops_place``   place_note, move_note, send_note, delete_note
- ``ops_vault``   vault_op and its lint sweep cache
"""

from __future__ import annotations

import sys
import types

# Re-exported for callers and tests that reach through ops.<name>.
from apo_engine import (
    archival_contract,
    config,
    core,
    deferred as index_deferred,
    git_catalog,
    git_contract,
    git_sync,
    links,
    note_lint,
    search_contract,
    okf as apo_okf,
    vault_contracts,
    vault_desk,
    vault_project,
    vaults,
)
from apo_engine.agent_args import (
    project_frontmatter,
    resolve_body_text,
    resolve_top_k,
    resolve_where,
    shape_note_read,
)
from apo_engine.atomic_io import atomic_write_bytes, atomic_write_text, note_write_lock
from apo_engine.chunk_anchor import materialize_ops_chunk_hashes, resolve_chunk_anchor
from apo_engine.json_patch import apply_json_patch
from apo_engine.markdown_patch import (
    PatchError,
    apply_append,
    apply_patch,
    find_section,
    minimal_note_stub,
    normalize_lines,
    section_from_chunk,
)
from apo_engine.mcp_backend import shape_search_hits
from apo_engine.note_format import (
    MUTATOR_SUFFIXES,
    ensure_indexed_path,
    is_contract_schema_path,
    is_json_catalog,
    is_mmd_note,
    is_markdown_note,
    is_mutator_path,
    has_blocked_dot_segment,
    is_exposed_note_rel,
    is_yaml_note,
)
from apo_engine.patch_ops import ops_to_dicts
from apo_engine.path_ref import (
    PathRefError,
    merge_vault_arg,
    peel_path_ref,
    qualified_path as _qualified_path,
)
from apo_engine.validation_hints import flatten_patch_failure_error
from apo_engine.write_guard import (
    PathTouch,
    WriteRegions,
    attach_region_hashes,
    check_write_precondition,
    classify_append_regions,
    classify_patch_regions,
    content_hash as region_content_hash,
    snapshot_from_content,
)
from apo_engine.yaml_patch import apply_yaml_patch
from apo_engine import (
    ops_common,
    ops_admin,
    ops_guard,
    ops_search,
    ops_read,
    ops_mutate,
    ops_place,
    ops_vault,
)
from apo_engine.ops_common import (
    OpsError,
    _err,
    _sqlite_index_err,
    _binding,
    _load_bindings,
    _resolve_binding_and_rel,
    _stamp_qualified,
    _safe_resolve,
    _require_note_path,
    _mtime,
    _io_error,
    _top_level_dirs,
    _FOLDER_TIP,
    _attach_tip,
    _attach_folder_tip,
    _attach_flaws,
    _missing_folder_warning,
)
from apo_engine.ops_admin import (
    WATCH_PID_FILE,
    _WATCHER_TIP,
    watcher_status,
    index_visibility,
    reindex,
    _attach_watcher_tip,
    stats,
    index_health,
    git_sync_op,
    list_refs_op,
)
from apo_engine.ops_guard import (
    _check_mtime,
    _guard_write,
    _enqueue_index,
    _enqueue_purge,
    _MTIME_TIP,
    _recent_touches,
    _touches_lock,
    _RECENT_TOUCH_TTL_S,
    _recent_writes,
    _write_key,
    _reject_if_ref,
    _prune_recent_touches,
    _record_path_touch,
    _attach_mtime_tip,
    _prepare_write_content,
    _attach_archival_write_flaws,
    _finalize_write,
)
from apo_engine.ops_search import (
    _attach_search_size_tips,
    _dedupe_vault_names,
    _resolve_search_bindings,
    _search_degraded_warning,
    _search_one_vault,
    _search_folder_pairs,
    _search_at_ref,
    _SEARCH_FANOUT_WORKERS,
    _search_fanout_pool_lock,
    _new_search_fanout_pool,
    _reset_search_fanout_pool,
    _run_search_pairs,
    search,
    filter_notes,
)
from apo_engine.ops_read import (
    _build_toc_from_blob,
    _apply_read_frontmatter,
    _short_title,
    _chunk_nav,
    _resolve_sibling,
    _table_from_file,
    _build_toc,
    _read_from_chunk,
    _attach_structured_table,
    read_note,
    _read_note_at_ref,
    graph_export,
    _str_or_none,
    expand_chunk,
    backlinks,
    _unresolved_node,
    graph_neighbors,
    _parse_history_bound,
    history,
)
from apo_engine.ops_mutate import (
    _write_atomic,
    _assemble_structured_note,
    _write_body_args_provided,
    write_note,
    append_note,
    _TableOpError,
    _table_ops_present,
    _locate_table,
    _row_content_hash_map,
    _fresh_row_hashes,
    _find_row_index,
    _col_index,
    _splice_table,
    _apply_one_table_op,
    _apply_replace_table,
    _apply_alter_schema,
    _apply_table_ops,
    patch_note,
    _PATCH_NOTES_MAX_ITEMS,
    _op_to_dict,
    _all_place_ops,
    _dispatch_place_op,
    patch_entry,
    patch_notes,
)
from apo_engine.ops_place import (
    _send_allow_roots,
    _resolve_send_src,
    place_note,
    move_note,
    _copy_into_vault,
    send_note,
    delete_note,
)
from apo_engine.ops_vault import (
    _lint_sweep_cache,
    _lint_sweep_cache_lock,
    _LINT_SWEEP_CACHE_TTL,
    _lint_cache_fingerprint,
    _vault_lint_merged_payload,
    _vault_row,
    vault_op,
)

_ROLE_MODULES = (
    ops_common,
    ops_admin,
    ops_guard,
    ops_search,
    ops_read,
    ops_mutate,
    ops_place,
    ops_vault,
)


class _OpsModule(types.ModuleType):
    """Keep ``ops.<name>`` the one patch point after the split.

    Callers and tests do ``monkeypatch.setattr(ops, "_guard_write", ...)``. The
    functions that call it now live in other modules and hold their own binding.
    Forward each assignment to every role module that holds the name.
    """

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for module in _ROLE_MODULES:
            if name in module.__dict__:
                setattr(module, name, value)


sys.modules[__name__].__class__ = _OpsModule


def __getattr__(name: str):
    # Rebound by ops_search._reset_search_fanout_pool, so read it live.
    if name == "_search_fanout_pool":
        return ops_search._search_fanout_pool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
