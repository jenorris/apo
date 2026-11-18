"""0.39.3 MCP error-surface and metrics regressions.

Covers: truthful tool metrics (ToolResult unwrap, tool_error label, metrics can
never fail a call), io_error on the write path, replace_text guards, patch_note
no-op / empty / partial responses, op-level validation hints and conservative
argument coercion.

The end-to-end tests drive the real server through FastMCP's in-memory client
under the hermetic conftest fixtures (metrics land in the per-test runtime dir).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import ValidationError as PydanticValidationError
from pydantic import create_model

from apo_engine import config, metrics_backend, ops, ops_place, tool_metrics
from apo_engine.arg_coercion import coerce_arguments, optional_non_nullable
from apo_engine.markdown_patch import apply_patch
from apo_engine.patch_ops import PatchNoteOp, ReplaceTextOp
from apo_engine.tool_metrics_middleware import ToolMetricsMiddleware, _result_payload
from apo_engine.validation_hints import format_tool_validation_error
from apo_engine.write_guard import file_region_hashes

_SERVER = Path(__file__).resolve().parents[1] / "src" / "apo_engine" / "mcp" / "server.py"
_COLLECTION = "err_surface_test"

_NOTE = "---\ntitle: N\nstatus: active\n---\n\n# N\n\n## Log\n\n- one\n"


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vault"
    (root / "areas").mkdir(parents=True)
    monkeypatch.setattr(config, "NOTES_ROOT", root)
    monkeypatch.setattr(config, "INDEX_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "COLLECTION", _COLLECTION)
    monkeypatch.setattr(config, "OKF_CONTRACT", "")
    monkeypatch.setattr(config, "OKF_ENFORCEMENT", "off")
    ops._recent_touches.clear()
    return root


def _seed(root: Path, rel: str, text: str = _NOTE) -> float:
    full = root / rel
    full.write_text(text, encoding="utf-8")
    old = time.time() - 100
    os.utime(full, (old, old))
    return full.stat().st_mtime


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The real MCP server module over a tmp vault, metrics in the tmp runtime dir."""
    root = tmp_path / "vault"
    root.mkdir()
    # apo_engine is already imported (conftest), so bind the vault on config too.
    monkeypatch.setattr(config, "NOTES_ROOT", root)
    monkeypatch.setattr(config, "INDEX_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "COLLECTION", _COLLECTION)
    monkeypatch.setattr(config, "OKF_CONTRACT", "")
    monkeypatch.setattr(config, "OKF_ENFORCEMENT", "off")
    ops._recent_touches.clear()
    monkeypatch.setenv("APO_NOTES_ROOT", str(root))
    monkeypatch.setenv("APO_INDEX", str(tmp_path / "index.db"))
    monkeypatch.setenv("APO_COLLECTION", _COLLECTION)
    monkeypatch.setenv("APO_TOOL_METRICS", "1")
    monkeypatch.delenv("APO_METRICS_BACKEND", raising=False)
    monkeypatch.delenv("APO_MCP_LEAN", raising=False)
    # Hermetic guard: metrics must resolve inside this test's tmp dir, never ~/.apo.
    store = metrics_backend.resolve_store_config(root).path.resolve()
    assert tmp_path.resolve() in store.parents, store
    assert tmp_path.resolve() in Path(tool_metrics.DEFERRED_DIR).resolve().parents

    spec = importlib.util.spec_from_file_location("apo_mcp_err_surface", _SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.vault_root = root
    mod.metrics_db = store
    return mod


def _call(mod, tool: str, args: dict):
    async def run():
        async with Client(mod.mcp) as client:
            return await client.call_tool(tool, args, raise_on_error=False)

    return asyncio.run(run())


def _payload(result) -> dict:
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def _error_text(result) -> str:
    assert result.is_error, _payload(result)
    return result.content[0].text


def _events(mod, tool: str) -> list[dict]:
    return tool_metrics.read_events(_COLLECTION, tool=tool, path=mod.metrics_db)


# --------------------------------------------------------------------------- #
# 1. Metrics: app-level failures are recorded ok=0 with their error code
# --------------------------------------------------------------------------- #
def test_failing_patch_note_is_recorded_as_failure_end_to_end(server):
    (server.vault_root / "n.md").write_text(_NOTE, encoding="utf-8")

    bad = _payload(
        _call(
            server,
            "patch_note",
            {"path": "n.md", "ops": [{"op": "replace_text", "find": "absent", "replace": "x"}]},
        )
    )
    assert bad["ok"] is False and bad["error"] == "match_not_found"
    good = _payload(
        _call(
            server,
            "patch_note",
            {"path": "n.md", "ops": [{"op": "set_field", "field": "status", "value": "done"}]},
        )
    )
    assert good["ok"] is True

    events = _events(server, "patch_note")
    assert len(events) == 2, events
    failed = [e for e in events if not e["ok"]]
    assert len(failed) == 1, events
    assert failed[0]["error"] == "match_not_found"
    passed = next(e for e in events if e["ok"])
    assert not passed.get("error")
    # resp_bytes is the tool payload size, not the repr of a ToolResult wrapper.
    assert failed[0]["resp_bytes"] == tool_metrics.estimate_bytes(bad)


def test_result_payload_unwraps_tool_result_shapes():
    assert _result_payload({"ok": False}) == {"ok": False}
    structured = MagicMock(structured_content={"ok": False, "error": "not_found"})
    assert _result_payload(structured)["error"] == "not_found"
    block = MagicMock()
    block.text = '{"ok": false, "error": "stale_write"}'
    text_only = MagicMock(structured_content=None, content=[block])
    assert _result_payload(text_only)["error"] == "stale_write"
    block.text = "not json"
    assert _result_payload(text_only) is text_only


# --------------------------------------------------------------------------- #
# 2. Metrics: ToolError labelling; metrics can never fail the call
# --------------------------------------------------------------------------- #
def _middleware_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.message = MagicMock()
    ctx.message.name = "read_note"
    ctx.message.arguments = {"path": "n.md"}
    return ctx


def _capture_record_call(monkeypatch) -> list[dict]:
    recorded: list[dict] = []
    monkeypatch.setattr(tool_metrics, "record_call", lambda **kw: recorded.append(kw))
    return recorded


def test_tool_error_without_validation_shape_is_tool_error(monkeypatch):
    recorded = _capture_record_call(monkeypatch)
    mw = ToolMetricsMiddleware(collection="c")
    with pytest.raises(ToolError):
        asyncio.run(
            mw.on_call_tool(_middleware_ctx(), AsyncMock(side_effect=ToolError("boom")))
        )
    assert recorded[0]["error"] == "tool_error"
    assert "error_shape" not in recorded[0]["flags"]


def test_tool_error_with_validation_shape_is_validation_error(monkeypatch):
    recorded = _capture_record_call(monkeypatch)
    model = create_model("Args", path=(str, ...))
    err = ToolError("bad args")
    try:
        model.model_validate({})
    except PydanticValidationError as e:
        err.__cause__ = e
    mw = ToolMetricsMiddleware(collection="c")
    with pytest.raises(ToolError):
        asyncio.run(mw.on_call_tool(_middleware_ctx(), AsyncMock(side_effect=err)))
    assert recorded[0]["error"] == "validation_error"
    assert recorded[0]["flags"]["error_shape"] == ["missing:path"]


def test_metrics_exception_does_not_fail_the_tool_call(monkeypatch):
    def boom(**_kw):
        raise RuntimeError("metrics store down")

    monkeypatch.setattr(tool_metrics, "record_call", boom)
    mw = ToolMetricsMiddleware(collection="c")
    out = asyncio.run(
        mw.on_call_tool(_middleware_ctx(), AsyncMock(return_value={"ok": True}))
    )
    assert out == {"ok": True}
    # ...and it must not replace the tool's own error either.
    with pytest.raises(ToolError, match="boom"):
        asyncio.run(
            mw.on_call_tool(_middleware_ctx(), AsyncMock(side_effect=ToolError("boom")))
        )


# --------------------------------------------------------------------------- #
# 3. io_error instead of a raw OSError with an absolute temp path
# --------------------------------------------------------------------------- #
def _deny_writes(monkeypatch) -> None:
    def deny(path, content, **_kw):
        raise PermissionError(13, "Permission denied", f"{path.parent}/.{path.name}.ab12.tmp")

    monkeypatch.setattr(ops, "atomic_write_text", deny)


def _assert_io_error(out: dict, vault: Path, rel: str) -> None:
    assert out["ok"] is False and out["error"] == "io_error", out
    assert "Permission denied" in out["message"]
    assert rel in out["message"]
    assert str(vault) not in json.dumps(out), out


def test_write_note_oserror_is_io_error(vault, monkeypatch):
    _deny_writes(monkeypatch)
    out = ops.write_note("areas/new.md", "# New\n")
    _assert_io_error(out, vault, "areas/new.md")
    assert out["path"] == "areas/new.md"
    _assert_io_error(ops.write_note("areas/data.json", "{}"), vault, "areas/data.json")


def test_append_note_oserror_is_io_error(vault, monkeypatch):
    _seed(vault, "areas/n.md")
    _deny_writes(monkeypatch)
    _assert_io_error(ops.append_note("areas/n.md", "- two", heading="Log"), vault, "areas/n.md")
    _assert_io_error(
        ops.append_note("areas/made.md", "- two", create=True), vault, "areas/made.md"
    )


def test_patch_note_oserror_is_io_error(vault, monkeypatch):
    _seed(vault, "areas/n.md")
    _deny_writes(monkeypatch)
    out = ops.patch_note("areas/n.md", [{"op": "set_field", "field": "status", "value": "x"}])
    _assert_io_error(out, vault, "areas/n.md")


def test_copy_into_vault_oserror_is_io_error(vault, tmp_path, monkeypatch):
    src = tmp_path / "host.md"
    src.write_text("# Host\n", encoding="utf-8")
    monkeypatch.setattr(config, "SEND_ALLOW_ROOTS", str(tmp_path), raising=False)
    monkeypatch.setenv("APO_SEND_ALLOW_ROOTS", str(tmp_path))
    _deny_writes(monkeypatch)
    out = ops.place_note(str(src), "areas/host.md")
    assert out["ok"] is False and out["error"] == "io_error", out
    assert out["dst"] == "areas/host.md"
    assert str(vault) not in out["message"]


def test_move_note_oserror_is_io_error(vault, monkeypatch):
    _seed(vault, "areas/n.md")

    def deny(_src, _dst):
        raise PermissionError(13, "Permission denied")

    with monkeypatch.context() as scoped:
        scoped.setattr(ops_place.os, "replace", deny)
        out = ops.move_note("areas/n.md", "areas/moved.md")
    assert out["ok"] is False and out["error"] == "io_error", out
    assert out["src"] == "areas/n.md" and out["dst"] == "areas/moved.md"
    assert "areas/moved.md" in out["message"] and str(vault) not in out["message"]


def test_io_error_scrubs_absolute_paths_from_custom_oserror_text(vault):
    full = vault / "areas" / "n.md"
    out = ops._io_error(OSError(1, f"refusing to replace symlink {full}"), full, "areas/n.md")
    assert out["error"] == "io_error" and out["path"] == "areas/n.md"
    assert out["message"] == "cannot write areas/n.md: refusing to replace symlink areas/n.md"


# --------------------------------------------------------------------------- #
# 4. replace_text: non-empty find, count >= 1
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "op, needle",
    [
        ({"find": "", "replace": "x"}, "non-empty find"),
        ({"find": "one", "replace": "x", "count": 0}, "count must be >= 1"),
        ({"find": "one", "replace": "x", "count": -1}, "count must be >= 1"),
    ],
)
def test_replace_text_guards(op, needle):
    op = {"op": "replace_text", **op}
    with pytest.raises(PydanticValidationError, match=needle):
        ReplaceTextOp.model_validate(op)
    # Non-MCP callers (plain dict ops) hit the same guard in the apply path.
    result = apply_patch(_NOTE, [op], strict=True)
    assert result.ok is False
    assert result.error["code"] == "invalid_op"
    assert needle in result.error["message"]
    assert result.content == _NOTE


def test_replace_text_guards_do_not_change_the_published_schema():
    props = ReplaceTextOp.model_json_schema()["properties"]
    assert "minLength" not in props["find"]
    assert "minimum" not in props["count"]


# --------------------------------------------------------------------------- #
# 5. patch_note: empty ops rejected; no-op patches do not rewrite the file
# --------------------------------------------------------------------------- #
def test_patch_note_empty_ops_is_bad_request_and_does_not_write(vault):
    mtime = _seed(vault, "areas/n.md")
    out = ops.patch_note("areas/n.md", [])
    assert out["ok"] is False and out["error"] == "bad_request", out
    assert (vault / "areas/n.md").stat().st_mtime == mtime
    assert ops.patch_entry(path="areas/n.md", ops=[])["error"] == "bad_request"


@pytest.mark.parametrize(
    "op",
    [
        {"op": "set_field", "field": "status", "value": "active"},
        {"op": "replace_text", "find": "- one", "replace": "- one"},
    ],
)
def test_patch_note_noop_skips_write_and_keeps_mtime(vault, op):
    mtime = _seed(vault, "areas/n.md")
    full = vault / "areas/n.md"

    out = ops.patch_note("areas/n.md", [op], expected_mtime=mtime)

    assert out["ok"] is True and out["noop"] is True, out
    assert out["applied"] == 1
    assert full.read_text(encoding="utf-8") == _NOTE
    assert full.stat().st_mtime == mtime
    # The response still describes the file on disk.
    assert out["mtime"] == mtime
    assert out["bytes"] == full.stat().st_size
    regions = file_region_hashes(_NOTE)
    assert out["frontmatter_hash"] == regions.frontmatter_hash
    assert out["body_hash"] == regions.body_hash
    # A second writer holding the same mtime is not invalidated.
    again = ops.patch_note(
        "areas/n.md",
        [{"op": "set_field", "field": "status", "value": "done"}],
        expected_mtime=mtime,
    )
    assert again["ok"] is True and "noop" not in again, again
    assert "status: done" in full.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 8. Partial patch: the first failure is visible at top level
# --------------------------------------------------------------------------- #
def test_partial_patch_surfaces_first_error(vault):
    _seed(vault, "areas/n.md")
    out = ops.patch_note(
        "areas/n.md",
        [
            {"op": "set_field", "field": "status", "value": "done"},
            {"op": "replace_text", "find": "absent", "replace": "x"},
            {"op": "replace_text", "find": "also absent", "replace": "x"},
        ],
    )
    assert out["ok"] is True and out["partial"] is True and out["failed"] == 2, out
    assert "partial patch" in out["warning"]
    assert out["op_index"] == 1
    assert out["first_error"]["code"] == "match_not_found"
    assert "absent" in out["first_error"]["message"]

    clean = ops.patch_note("areas/n.md", [{"op": "set_field", "field": "status", "value": "x"}])
    assert clean["ok"] is True
    assert not {"first_error", "op_index"} & set(clean)
    assert "partial patch" not in clean.get("warning", "")


# --------------------------------------------------------------------------- #
# 9. Validation hints
# --------------------------------------------------------------------------- #
_PatchArgs = create_model("_PatchArgs", ops=(list[PatchNoteOp], ...))


def _ops_error(ops_arg: list[dict]) -> Exception:
    """A FastMCP-shaped ValidationError whose locs start at ``ops`` like the server's."""
    from fastmcp.exceptions import ValidationError as FastMCPValidationError

    try:
        _PatchArgs.model_validate({"ops": ops_arg})
    except PydanticValidationError as e:
        wrapped = FastMCPValidationError(str(e))
        wrapped.__cause__ = e
        return wrapped
    raise AssertionError("expected validation failure")


def test_unknown_key_on_op_is_answered_from_the_op_model():
    msg = format_tool_validation_error(
        "patch_note",
        _ops_error(
            [
                {"op": "set_field", "field": "a", "value": 1},
                {"op": "set_field", "field": "status", "valu": "x", "text": "y"},
            ]
        ),
    )
    assert "ops[1] (set_field): unknown key 'text'; valid keys: field, value." in msg
    assert "unknown key 'valu'" in msg and "Did you mean 'value'?" in msg
    assert "not top-level" not in msg


@pytest.mark.parametrize("bad, nearest", [("replace", "replace_text"), ("update", None)])
def test_unknown_op_value_names_index_and_valid_ops(bad, nearest):
    msg = format_tool_validation_error(
        "patch_note",
        _ops_error([{"op": "set_field", "field": "a"}, {"op": bad, "find": "a"}]),
    )
    assert f"patch_note ops[1]: unknown op {bad!r}; valid ops:" in msg
    for name in ("set_field", "replace_text", "replace_section", "append_eof", "place"):
        assert name in msg
    if nearest:
        assert f"Did you mean {nearest!r}?" in msg
    assert "errors.pydantic.dev" not in msg


def test_value_error_hint_names_the_op():
    msg = format_tool_validation_error(
        "patch_note",
        _ops_error([{"op": "replace_section", "text": "body"}]),
    )
    assert msg.startswith("patch_note ops[0] (replace_section): ")
    assert "requires heading|target or chunk_hash" in msg
    empty_find = format_tool_validation_error(
        "patch_note", _ops_error([{"op": "replace_text", "find": ""}])
    )
    assert "ops[0] (replace_text): replace_text requires a non-empty find" in empty_find


def test_unknown_argument_fallback_lists_valid_parameters():
    class Fake(Exception):
        def errors(self, include_url: bool = True):
            return [
                {"type": "unexpected_keyword_argument", "loc": ("pth",), "msg": "x", "input": 1}
            ]

    exc = Exception("err")
    exc.__cause__ = Fake()
    msg = format_tool_validation_error(
        "read_note", exc, valid_params=["path", "chunk_hash", "heading"]
    )
    assert "read_note does not accept argument 'pth'" in msg
    assert "Valid arguments: path, chunk_hash, heading." in msg
    assert "Did you mean 'path'?" in msg
    assert "GetMcpTools" not in msg
    assert "GetMcpTools" not in format_tool_validation_error("read_note", exc)


def test_unknown_argument_hint_end_to_end_lists_real_parameters(server):
    text = _error_text(_call(server, "read_note", {"pth": "n.md"}))
    assert "does not accept argument 'pth'" in text
    assert "Valid arguments: path," in text and "Did you mean 'path'?" in text
    op_text = _error_text(
        _call(
            server,
            "patch_note",
            {"path": "n.md", "ops": [{"op": "set_field", "field": "a", "text": "b"}]},
        )
    )
    assert "ops[0] (set_field): unknown key 'text'" in op_text


# --------------------------------------------------------------------------- #
# 10. Conservative argument coercion
# --------------------------------------------------------------------------- #
_OP = {"op": "set_field", "field": "status", "value": "done"}

_COERCION_CASES = [
    # (tool, arguments, droppable None keys, expected arguments, used_alias)
    ("search_notes", {"query": "q", "k": 3}, (), {"query": "q", "limit": 3}, True),
    ("search_notes", {"query": "q", "max_results": 3}, (), {"query": "q", "limit": 3}, True),
    ("filter_notes", {"num_results": 3}, (), {"limit": 3}, True),
    ("search", {"query": "q", "k": 3}, (), {"query": "q", "limit": 3}, True),
    ("patch_note", {"path": "n.md", "operations": [_OP]}, (), {"path": "n.md", "ops": [_OP]}, True),
    ("patch_note", {"edits": [_OP]}, (), {"ops": [_OP]}, True),
    ("patch_note", {"updates": [_OP]}, (), {"ops": [_OP]}, True),
    ("patch_note", {"ops": json.dumps([_OP])}, (), {"ops": [_OP]}, False),
    ("patch_note", {"ops": _OP}, (), {"ops": [_OP]}, False),
    ("patch_note", {"ops": json.dumps(_OP)}, (), {"ops": [_OP]}, False),
    ("patch_note", {"operations": json.dumps(_OP)}, (), {"ops": [_OP]}, True),
    ("filter_notes", {"where": '{"status": "active"}'}, (), {"where": {"status": "active"}}, False),
    ("vault", {"request": '{"action": "list"}'}, (), {"request": {"action": "list"}}, False),
    ("write_note", {"path": "n.md", "body": "x"}, (), {"path": "n.md", "content": "x"}, True),
    ("write_note", {"path": "n.md", "text": "x"}, (), {"path": "n.md", "content": "x"}, True),
    ("append_note", {"path": "n.md", "content": "x"}, (), {"path": "n.md", "text": "x"}, True),
    ("append_note", {"path": "n.md", "body": "x"}, (), {"path": "n.md", "text": "x"}, True),
    ("filter_notes", {"limit": None, "folder": "a"}, ("limit",), {"folder": "a"}, False),
    ("filter_notes", {"limit": None, "k": 2}, ("limit",), {"limit": 2}, True),
]

_NO_REWRITE_CASES = [
    # Alias and canonical name both given: normal validation decides.
    ("search_notes", {"query": "q", "k": 3, "limit": 5}),
    ("patch_note", {"ops": [_OP], "operations": [_OP]}),
    ("write_note", {"path": "n.md", "content": "a", "text": "b"}),
    ("append_note", {"path": "n.md", "text": "a", "content": "b"}),
    # Two aliases for one parameter: ambiguous.
    ("search_notes", {"query": "q", "k": 3, "max_results": 5}),
    ("write_note", {"path": "n.md", "body": "a", "text": "b"}),
    # Malformed or wrong-shaped JSON strings fall through untouched.
    ("patch_note", {"ops": "[{op: set_field"}),
    ("patch_note", {"ops": '"just a string"'}),
    ("filter_notes", {"where": "{status: active"}),
    ("filter_notes", {"where": "[1, 2]"}),
    ("vault", {"request": "list"}),
    # Out of scope: other tools, other parameters, nullable or required None.
    ("read_note", {"path": "n.md", "max_bytes": 10, "offset": 2}),
    ("read_note", {"path": "n.md", "k": 3}),
    ("write_note", {"path": "n.md", "content": "x", "overwrite": True, "mode": "w"}),
    ("patch_note", {"ops": [{"op": "replace", "find": "a"}]}),
    ("apo_admin", {"action": "list", "parameters": '{"a": 1}', "k": 1}),
    ("search_notes", {"query": "q", "limit": None}),
]


@pytest.mark.parametrize("tool, args, droppable, expected, aliased", _COERCION_CASES)
def test_coerce_arguments_rules(tool, args, droppable, expected, aliased):
    before = json.dumps(args, sort_keys=True)
    out, used_alias = coerce_arguments(tool, args, droppable_none=droppable)
    assert out == expected
    assert used_alias is aliased
    assert json.dumps(args, sort_keys=True) == before, "input must not be mutated"
    assert bool(tool_metrics.extract_arg_flags(args, tool=tool).get("used_alias")) is aliased


@pytest.mark.parametrize("tool, args", _NO_REWRITE_CASES)
def test_coerce_arguments_leaves_ambiguous_or_out_of_scope_calls_alone(tool, args):
    out, used_alias = coerce_arguments(tool, args)
    assert out is args
    assert used_alias is False


def test_optional_non_nullable_reads_the_tool_schema():
    schema = {
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 20},
            "heading": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
            "anything": {"default": None},
            "mode": {"type": ["string", "null"]},
        },
        "required": ["query"],
    }
    assert optional_non_nullable(schema) == frozenset({"limit"})
    assert optional_non_nullable(None) == frozenset()


def test_ops_count_flag_sees_coerced_ops():
    flags = tool_metrics.extract_arg_flags({"operations": json.dumps([_OP, _OP])}, tool="patch_note")
    assert flags["ops_count"] == 2 and flags["used_alias"] is True


def test_coercion_end_to_end_near_misses_succeed(server):
    root = server.vault_root

    wrote = _payload(_call(server, "write_note", {"path": "n.md", "body": _NOTE}))
    assert wrote["ok"] is True, wrote
    assert (root / "n.md").read_text(encoding="utf-8").startswith("---\ntitle: N")

    appended = _payload(
        _call(server, "append_note", {"path": "n.md", "content": "- two", "heading": "Log"})
    )
    assert appended["ok"] is True, appended
    assert "- two" in (root / "n.md").read_text(encoding="utf-8")

    for args in (
        {"path": "n.md", "operations": [_OP]},
        {"path": "n.md", "ops": json.dumps([{**_OP, "value": "a"}])},
        {"path": "n.md", "ops": {**_OP, "value": "b"}},
        # null for optional non-nullable parameters is dropped.
        {"path": "n.md", "ops": [{**_OP, "value": "c"}], "strict": None, "vault": None},
    ):
        patched = _payload(_call(server, "patch_note", args))
        assert patched["ok"] is True, (args, patched)
    assert "status: c" in (root / "n.md").read_text(encoding="utf-8")

    for tool, args in (
        ("filter_notes", {"where": '{"status": "c"}', "k": 1, "offset": None}),
        ("vault", {"request": '{"action": "list"}'}),
    ):
        result = _call(server, tool, args)
        assert not result.is_error, result.content[0].text

    (event,) = _events(server, "write_note")
    assert event["ok"] and event.get("used_alias") is True


@pytest.mark.parametrize(
    "tool, args",
    [
        ("write_note", {"path": "n.md", "content": "a", "text": "b"}),
        ("append_note", {"path": "n.md", "text": "a", "body": "b"}),
        ("patch_note", {"path": "n.md", "ops": [_OP], "operations": [_OP]}),
        ("filter_notes", {"limit": 1, "k": 2}),
        ("patch_note", {"path": "n.md", "ops": "[{op: set_field"}),
        ("filter_notes", {"where": "{status: active"}),
    ],
)
def test_coercion_end_to_end_ambiguous_calls_still_fail_validation(server, tool, args):
    (server.vault_root / "n.md").write_text(_NOTE, encoding="utf-8")
    result = _call(server, tool, args)
    assert result.is_error
    assert (server.vault_root / "n.md").read_text(encoding="utf-8") == _NOTE
    (event,) = _events(server, tool)
    assert event["ok"] is False and event["error"] == "validation_error"
