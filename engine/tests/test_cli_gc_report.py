"""apo-engine doctor --gc-report: read-only runtime artifact report, bounded --apply."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apo_engine import cli, vaults


@pytest.fixture
def runtime(tmp_path):
    rt = tmp_path / "apo"
    rt.mkdir()
    live_db = rt / "index-live.db"
    for name in ("index-live.db", "index-live.db-wal", "index-live-refs.db", "index-gone.db"):
        (rt / name).write_bytes(b"x" * 100)
    (rt / "deferred-live.json").write_text('["a.md"]')
    (rt / "deferred-gone-empty.json").write_text("[]")
    (rt / "purge-gone-full.json").write_text('["b.md"]')
    (rt / "purge-gone-blank.json").write_text("")
    store = rt / "vault-ids.json"
    store.write_text(
        json.dumps({"/private/var/folders/xx/T/tmpabc/vault": "a1", "/Users/me/real-vault": "b2"})
    )
    binding = vaults.VaultBinding("live", tmp_path / "live", live_db, "live")
    return rt, store, {"live": binding}


def _report(runtime, **kw):
    rt, store, bindings = runtime
    return cli.gc_report(runtime_dir=rt, queue_dir=rt, id_store=store, bindings=bindings, **kw)


def test_report_lists_unreferenced_artifacts_and_deletes_nothing(runtime):
    rt, store, _ = runtime
    before = sorted(p.name for p in rt.iterdir())
    out = _report(runtime)
    assert [Path(e["path"]).name for e in out["index_dbs"]] == ["index-gone.db"]
    assert out["index_dbs"][0]["bytes"] == 100
    queues = {Path(e["path"]).name: e["empty"] for e in out["queues"]}
    assert queues == {
        "deferred-gone-empty.json": True,
        "purge-gone-blank.json": True,
        "purge-gone-full.json": False,
    }
    assert out["stale_vault_ids"]["paths"] == ["/private/var/folders/xx/T/tmpabc/vault"]
    assert sorted(p.name for p in rt.iterdir()) == before


def test_unreferenced_index_size_includes_wal(runtime):
    rt, _, _ = runtime
    (rt / "index-gone.db-wal").write_bytes(b"w" * 50)
    assert _report(runtime)["index_dbs"][0]["bytes"] == 150


def test_apply_removes_only_stale_ids_and_empty_unreferenced_queues(runtime):
    rt, store, _ = runtime
    out = _report(runtime, apply=True)
    assert len(out["removed"]["queues"]) == 2
    assert json.loads(store.read_text()) == {"/Users/me/real-vault": "b2"}
    left = {p.name for p in rt.iterdir()}
    assert {"index-gone.db", "index-live.db", "deferred-live.json", "purge-gone-full.json"} <= left
    assert not {"deferred-gone-empty.json", "purge-gone-blank.json"} & left


def test_doctor_apply_requires_gc_report(capsys):
    assert cli.main(["doctor", "--apply"]) == 2
    assert "--gc-report" in capsys.readouterr().err
