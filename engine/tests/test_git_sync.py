"""Git contract sync — commit/push/pull + never_commit gates."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apo_engine import config, git_sync, ops


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _write_contract(vault: Path, *, enabled: bool = True, extra: str = "") -> None:
    cfg = vault / "system" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    body = (
        "git_contract_version: '0.1'\n"
        "remote: 'https://example.com/r.git'\n"
        "host: local\n"
        "default_branch: main\n"
        "never_commit:\n"
        "  - '*.db'\n"
        "  - '.apo/'\n"
        "  - '.env'\n"
        "sync:\n"
        f"  enabled: {'true' if enabled else 'false'}\n"
        "  debounce_seconds: 2\n"
        "  pull_interval_seconds: 60\n"
        "  commit_message_template: "
        "'apo: sync {iso_local} · {path_count} paths · {top_folders}'\n"
        "  auto_push: true\n"
    )
    if extra:
        body += extra
    (cfg / "git-contract.schema.yaml").write_text(body, encoding="utf-8")


class GitSyncHelpersTest(unittest.TestCase):
    def test_never_commit_globs(self):
        pats = ("*.db", ".apo/", "**/Passport*.key", ".env")
        self.assertTrue(git_sync.path_never_commit("index.db", pats))
        self.assertTrue(git_sync.path_never_commit(".apo/git-sync-status.json", pats))
        self.assertTrue(git_sync.path_never_commit("secrets/PassportFoo.key", pats))
        self.assertTrue(git_sync.path_never_commit(".env", pats))
        self.assertFalse(git_sync.path_never_commit("inbox/a.md", pats))

    def test_format_commit_message(self):
        dt = datetime(2026, 8, 3, 0, 35, tzinfo=ZoneInfo("America/New_York"))
        msg = git_sync.format_commit_message("apo: sync {iso_local}", now=dt)
        self.assertEqual(msg, "apo: sync 2026-08-03 00:35 ET")

    def test_paths_summary_bits_top_folders_and_truncation(self):
        paths = [
            "areas/threads/a.md",
            "areas/threads/b.md",
            "areas/threads/c.md",
            "inbox/daily/2026-08-04.md",
            "inbox/daily/2026-08-03.md",
            "projects/apo/x.md",
            "resources/wiki/y.md",
            "system/config/z.md",
            "note.md",
        ]
        bits = git_sync.paths_summary_bits(paths)
        self.assertEqual(bits["path_count"], "9")
        self.assertEqual(bits["top_folders"], bits["paths_summary"])
        # areas/threads (3), inbox/daily (2), then alphabetical among count=1:
        # note.md, projects/apo, resources/wiki, system/config → top 3 by (-count, name)
        self.assertEqual(
            bits["top_folders"],
            "areas/threads, inbox/daily, note.md +3 more",
        )

    def test_format_commit_message_path_tokens(self):
        dt = datetime(2026, 8, 4, 14, 27, tzinfo=ZoneInfo("America/New_York"))
        tmpl = "apo: sync {iso_local} · {path_count} paths · {top_folders}"
        msg = git_sync.format_commit_message(
            tmpl,
            now=dt,
            paths=["areas/threads/a.md", "inbox/daily/d.md"],
        )
        self.assertEqual(
            msg,
            "apo: sync 2026-08-04 14:27 ET · 2 paths · areas/threads, inbox/daily",
        )
        # Unknown tokens stay literal
        self.assertIn(
            "{unknown}",
            git_sync.format_commit_message("x {unknown} {path_count}", paths=["a.md"]),
        )

    def test_commit_message_parts_agent_vs_template(self):
        paths = ["areas/threads/a.md"]
        tmpl = "apo: sync {iso_local} · {path_count} paths · {top_folders}"
        subj, body = git_sync.commit_message_parts(
            tmpl,
            message="agent: outcome",
            paths=paths,
        )
        self.assertEqual(subj, "agent: outcome")
        self.assertIn("Paths:", body)
        self.assertIn("- areas/threads/a.md", body)

        subj2, body2 = git_sync.commit_message_parts(tmpl, paths=paths)
        self.assertIn("1 paths", subj2)
        self.assertIn("areas/threads", subj2)
        self.assertEqual(body2, body)

    def test_paths_body_cap(self):
        paths = [f"f/{i}.md" for i in range(45)]
        body = git_sync.paths_body(paths)
        self.assertIn("… and 5 more", body)
        self.assertEqual(len([ln for ln in body.splitlines() if ln.startswith("- ")]), 40)


class _VaultRepoCase(unittest.TestCase):
    """Vault work tree pushed to a local bare ``origin``, sync contract on."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-gsync-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "note.md").write_text("# N\n\nv1\n", encoding="utf-8")
        _git(self.vault, "init", "-b", "main")
        _git(self.vault, "config", "user.email", "test@example.com")
        _git(self.vault, "config", "user.name", "Test")
        _git(self.vault, "add", "note.md")
        _git(self.vault, "commit", "-m", "initial")
        self.bare = self.tmp / "remote.git"
        _git(self.tmp, "init", "--bare", str(self.bare))
        _git(self.bare, "symbolic-ref", "HEAD", "refs/heads/main")
        _git(self.vault, "remote", "add", "origin", str(self.bare))
        _git(self.vault, "push", "-u", "origin", "main")
        _write_contract(self.vault, enabled=True)
        self._patches = [
            unittest.mock.patch.object(config, "NOTES_ROOT", self.vault),
            unittest.mock.patch.object(config, "INDEX_PATH", self.tmp / "index.db"),
            unittest.mock.patch.object(config, "COLLECTION", "gsync_test"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class GitSyncRepoTest(_VaultRepoCase):
    def test_status_reports_sync_enabled(self):
        # Ported from the deleted RPC-transport test — enabled-contract case
        # (test_sync_disabled_status below covers the disabled case).
        out = ops.git_sync_op("status")
        self.assertTrue(out["ok"], msg=out)
        self.assertTrue(out["sync_enabled"])

    def test_commit_push_template_and_never_commit(self):
        (self.vault / "note.md").write_text("# N\n\nv2\n", encoding="utf-8")
        (self.vault / "secret.db").write_text("nope", encoding="utf-8")
        (self.vault / ".apo").mkdir(exist_ok=True)
        (self.vault / ".apo" / "x.json").write_text("{}", encoding="utf-8")

        out = git_sync.commit_and_push(self.vault)
        self.assertTrue(out["ok"], msg=out)
        self.assertTrue(out["committed"])
        self.assertTrue(out["pushed"])
        self.assertTrue(out["message"].startswith("apo: sync "))
        self.assertIn("paths", out["message"])
        self.assertIn("note.md", out["message"])
        self.assertIn("note.md", out["paths"])
        self.assertNotIn("secret.db", out["paths"])

        log = _git(self.vault, "log", "-1", "--name-only", "--pretty=%B")
        self.assertIn("Paths:", log.stdout)
        self.assertIn("- note.md", log.stdout)
        self.assertNotIn("secret.db", log.stdout)

    def test_commit_agent_message_keeps_paths_body(self):
        (self.vault / "areas" / "threads").mkdir(parents=True)
        (self.vault / "areas" / "threads" / "t.md").write_text("x\n", encoding="utf-8")
        out = git_sync.commit_and_push(self.vault, message="agent: foo")
        self.assertTrue(out["ok"], msg=out)
        self.assertEqual(out["message"], "agent: foo")
        log = _git(self.vault, "log", "-1", "--pretty=%B")
        self.assertTrue(log.stdout.startswith("agent: foo\n"))
        self.assertIn("Paths:", log.stdout)
        self.assertIn("- areas/threads/t.md", log.stdout)

    def test_commits_paths_git_would_quote_or_glob(self):
        # core.quotePath C-escapes non-ASCII names in porcelain output, and
        # glob metacharacters are pathspec patterns unless passed literally.
        # Either one previously failed `git add` and blocked the whole vault.
        awkward = [
            "tasks/Open Items Triage \u2014 2026-04-28.md",
            "tasks/report [2026] draft.md",
            "tasks/caf\u00e9 notes.md",
        ]
        (self.vault / "tasks").mkdir()
        for rel in awkward:
            (self.vault / rel).write_text("x\n", encoding="utf-8")
        (self.vault / "note.md").write_text("# N\n\nv4\n", encoding="utf-8")

        out = git_sync.commit_and_push(self.vault)
        self.assertTrue(out["ok"], msg=out)
        self.assertTrue(out["committed"], msg=out)
        for rel in awkward:
            self.assertIn(rel, out["paths"])

        tracked = _git(self.vault, "ls-files", "-z").stdout.split("\0")
        dirty = _git(self.vault, "status", "--porcelain", "-z", "-u").stdout
        for rel in awkward:
            self.assertIn(rel, tracked)
            self.assertNotIn(rel, dirty)

    def test_stages_renames_without_consuming_origin_as_entry(self):
        _git(self.vault, "mv", "note.md", "renamed \u2014 note.md")
        paths = git_sync.list_stageable_paths(self.vault, ())
        self.assertIn("renamed \u2014 note.md", paths)
        self.assertNotIn("note.md", paths)

    def test_on_block_command_fires_once_per_block_episode(self):
        sentinel = self.tmp / "notified.log"
        _write_contract(
            self.vault,
            enabled=True,
            extra=f"  on_block_command: 'printf \"%s\\n\" \"$APO_SYNC_ERROR\" >> {sentinel}'\n",
        )

        git_sync._block(self.vault, "boom: push rejected")
        self.assertEqual(sentinel.read_text(encoding="utf-8").splitlines(), ["boom: push rejected"])

        # Still blocked → no repeat alert on every later tick.
        git_sync._block(self.vault, "boom: push rejected")
        self.assertEqual(sentinel.read_text(encoding="utf-8").splitlines(), ["boom: push rejected"])

        # Cleared, then blocked again → new episode, new alert.
        git_sync.clear_block(self.vault)
        git_sync._block(self.vault, "boom: second episode")
        self.assertEqual(
            sentinel.read_text(encoding="utf-8").splitlines(),
            ["boom: push rejected", "boom: second episode"],
        )

    def test_block_survives_failing_on_block_command(self):
        _write_contract(self.vault, enabled=True, extra="  on_block_command: 'exit 7'\n")
        st = git_sync._block(self.vault, "boom")
        self.assertEqual(st["state"], "blocked")
        self.assertTrue(git_sync.is_blocked(self.vault))

    def test_tool_message_override(self):
        (self.vault / "note.md").write_text("# N\n\nv3\n", encoding="utf-8")
        out = ops.git_sync_op("run", message="agent: batch sync")
        self.assertTrue(out["ok"], msg=out)
        self.assertEqual(out["message"], "agent: batch sync")

    def test_ff_only_pull_blocks_on_diverge(self):
        # Diverging remote tip (fetch into vault after remote advances)
        clone = self.tmp / "other"
        _git(self.tmp, "clone", str(self.bare), str(clone))
        _git(clone, "config", "user.email", "test@example.com")
        _git(clone, "config", "user.name", "Test")
        (clone / "note.md").write_text("# remote\n", encoding="utf-8")
        _git(clone, "add", "note.md")
        _git(clone, "commit", "-m", "remote change")
        _git(clone, "push", "origin", "HEAD:main")

        (self.vault / "note.md").write_text("# local diverge\n", encoding="utf-8")
        _git(self.vault, "add", "note.md")
        _git(self.vault, "commit", "-m", "local change")

        out = git_sync.pull_ff_only(self.vault)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "pull_failed")
        self.assertEqual(git_sync.read_status(self.vault)["state"], "blocked")

        # Further run refused until clear
        (self.vault / "extra.md").write_text("# x\n", encoding="utf-8")
        blocked = git_sync.commit_and_push(self.vault)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"], "blocked")

        cleared = ops.git_sync_op("clear_block")
        self.assertTrue(cleared["ok"])
        self.assertNotEqual(cleared["status"]["state"], "blocked")

    def test_rebase_recovers_from_diverge_and_pushes(self):
        # Remote advances with a file the local side never touches.
        clone = self.tmp / "other"
        _git(self.tmp, "clone", str(self.bare), str(clone))
        _git(clone, "config", "user.email", "test@example.com")
        _git(clone, "config", "user.name", "Test")
        (clone / "remote_only.md").write_text("# remote\n", encoding="utf-8")
        _git(clone, "add", "remote_only.md")
        _git(clone, "commit", "-m", "remote change")
        _git(clone, "push", "origin", "HEAD:main")

        # Local diverges with an unrelated file — same scenario that blocks pull_ff_only.
        (self.vault / "local_only.md").write_text("# local\n", encoding="utf-8")
        _git(self.vault, "add", "local_only.md")
        _git(self.vault, "commit", "-m", "local change")

        blocked = git_sync.pull_ff_only(self.vault)
        self.assertFalse(blocked["ok"])
        self.assertEqual(git_sync.read_status(self.vault)["state"], "blocked")

        cleared = git_sync.clear_block(self.vault)
        self.assertNotEqual(cleared["state"], "blocked")

        out = git_sync.rebase_onto_remote(self.vault)
        self.assertTrue(out["ok"], msg=out)
        self.assertTrue(out["rebased"])
        self.assertTrue(out["pushed"])
        self.assertEqual(git_sync.read_status(self.vault)["state"], "ok")

        self.assertTrue((self.vault / "remote_only.md").exists())
        self.assertTrue((self.vault / "local_only.md").exists())

        # Rebase kept push a fast-forward: bare tip == local HEAD, no force needed.
        local_head = _git(self.vault, "rev-parse", "HEAD").stdout.strip()
        remote_head = _git(self.bare, "rev-parse", "main").stdout.strip()
        self.assertEqual(local_head, remote_head)

    def test_rebase_conflict_aborts_cleanly_and_blocks(self):
        # Remote and local both edit note.md's only line — guaranteed conflict.
        clone = self.tmp / "other"
        _git(self.tmp, "clone", str(self.bare), str(clone))
        _git(clone, "config", "user.email", "test@example.com")
        _git(clone, "config", "user.name", "Test")
        (clone / "note.md").write_text("# remote change\n", encoding="utf-8")
        _git(clone, "add", "note.md")
        _git(clone, "commit", "-m", "remote edits note")
        _git(clone, "push", "origin", "HEAD:main")

        (self.vault / "note.md").write_text("# local change\n", encoding="utf-8")
        _git(self.vault, "add", "note.md")
        _git(self.vault, "commit", "-m", "local edits note")

        out = git_sync.rebase_onto_remote(self.vault)
        self.assertFalse(out["ok"], msg=out)
        self.assertEqual(out["error"], "rebase_conflict")
        self.assertEqual(git_sync.read_status(self.vault)["state"], "blocked")

        # Aborted, not left mid-rebase: no REBASE_HEAD, note.md clean (no unmerged
        # markers) — untracked .apo/ status file and system/ contract dir from
        # this test's own setup are expected noise, not rebase fallout.
        self.assertIsNone(git_sync.unsafe_git_state(self.vault))
        status = _git(self.vault, "status", "--porcelain")
        self.assertNotIn("note.md", status.stdout)
        self.assertNotIn("UU", status.stdout)
        self.assertEqual(
            (self.vault / "note.md").read_text(encoding="utf-8"), "# local change\n"
        )

        # Blocked is sticky, same as any other sync failure, until explicitly cleared.
        again = git_sync.rebase_onto_remote(self.vault)
        self.assertFalse(again["ok"])
        self.assertEqual(again["error"], "blocked")

    def test_sync_disabled_status(self):
        _write_contract(self.vault, enabled=False)
        st = ops.git_sync_op("status")
        self.assertTrue(st["ok"])
        self.assertFalse(st["sync_enabled"])

    def test_controller_debounce_commit(self):
        ctl = git_sync.VaultSyncController(self.vault, verbose=False)
        (self.vault / "note.md").write_text("# N\n\ndebounced\n", encoding="utf-8")
        ctl.note_apo_writes()
        self.assertTrue(ctl.pending_commit())
        # Force due immediately
        ctl._commit_due_at = 0.0  # noqa: SLF001 — test hook
        ctl.tick(index_busy=False)
        log = _git(self.vault, "log", "-1", "--pretty=%s")
        self.assertTrue(log.stdout.strip().startswith("apo: sync "))


# Port 9 (discard) on loopback: nothing listens, so git gets an immediate
# "Connection refused" — a real transient network failure, no mocking.
_DEAD_REMOTE = "http://127.0.0.1:9/x.git"


class GitSyncOfflineTest(_VaultRepoCase):
    """Transient network failures → ``offline`` + backoff retry, never ``blocked``."""

    def _go_offline(self) -> None:
        _git(self.vault, "remote", "set-url", "origin", _DEAD_REMOTE)

    def _go_online(self) -> None:
        _git(self.vault, "remote", "set-url", "origin", str(self.bare))

    def _make_due(self) -> None:
        st = git_sync.read_status(self.vault)
        st["next_retry_at"] = "2000-01-01T00:00:00+00:00"
        git_sync.write_status(self.vault, st)

    def _remote_head(self) -> str:
        return _git(self.bare, "rev-parse", "main").stdout.strip()

    def _local_head(self) -> str:
        return _git(self.vault, "rev-parse", "HEAD").stdout.strip()

    def test_classifier(self):
        transient = [
            "fatal: unable to access 'https://github.com/x/y.git/': Could not resolve host: github.com",
            "fatal: unable to access '…': Failed to connect to github.com port 443 after 75000 ms",
            "ssh: connect to host github.com port 22: Network is unreachable",
            "fatal: the remote end hung up unexpectedly",
            "git push: Operation timed out after 30s",
        ]
        for err in transient:
            self.assertTrue(git_sync._is_transient_network_error(err), err)
        blocking = [
            "fatal: Authentication failed for 'https://github.com/x/y.git/'",
            "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.",
            " ! [rejected]        main -> main (non-fast-forward)",
            "error: The following untracked working tree files would be overwritten by merge",
            "fatal: Not possible to fast-forward, aborting.",
            "unsafe_git_state: detached HEAD",
            "",
            None,
        ]
        for err in blocking:
            self.assertFalse(git_sync._is_transient_network_error(err), err)

    def test_push_to_dead_remote_goes_offline_not_blocked(self):
        sentinel = self.tmp / "notified.log"
        _write_contract(
            self.vault,
            enabled=True,
            extra=f"  on_block_command: 'echo x >> {sentinel}'\n",
        )
        self._go_offline()
        (self.vault / "note.md").write_text("# N\n\noffline 1\n", encoding="utf-8")
        out = git_sync.commit_and_push(self.vault)
        self.assertFalse(out["ok"], msg=out)
        self.assertEqual(out["error"], "offline")

        st = git_sync.read_status(self.vault)
        self.assertEqual(st["state"], "offline")
        self.assertFalse(git_sync.is_blocked(self.vault))
        self.assertIn("connect", st["error"].lower())
        self.assertEqual(st["retry_count"], 1)
        self.assertTrue(st["offline_since"])
        self.assertTrue(st["next_retry_at"])
        self.assertIsNone(st["blocked_at"])
        # Offline is not a block → no on_block_command alert.
        self.assertFalse(sentinel.exists())

        status = git_sync.run_action(self.vault, "status")
        self.assertTrue(status["offline"])
        self.assertEqual(status["retry_count"], 1)
        self.assertEqual(status["next_retry_at"], st["next_retry_at"])

    def test_commits_land_offline_and_backlog_pushes_on_recovery(self):
        self._go_offline()
        (self.vault / "note.md").write_text("# N\n\noffline 1\n", encoding="utf-8")
        git_sync.commit_and_push(self.vault)
        first_since = git_sync.read_status(self.vault)["offline_since"]

        # Second commit while backing off: lands locally, push deferred.
        (self.vault / "b.md").write_text("# b\n", encoding="utf-8")
        out = git_sync.commit_and_push(self.vault)
        self.assertTrue(out["ok"], msg=out)
        self.assertTrue(out["committed"])
        self.assertFalse(out["pushed"])
        self.assertEqual(out["push_deferred"], "offline")
        self.assertEqual(git_sync.read_status(self.vault)["state"], "offline")
        self.assertEqual(git_sync.commits_ahead_of_upstream(self.vault), 2)

        # A due retry against a still-dead remote stays offline and backs off.
        ctl = git_sync.VaultSyncController(self.vault, verbose=False)
        self._make_due()
        ctl.tick(index_busy=False)
        st = git_sync.read_status(self.vault)
        self.assertEqual(st["state"], "offline")
        self.assertEqual(st["retry_count"], 2)
        self.assertEqual(st["offline_since"], first_since)

        # Network back: the next due retry pulls, then pushes the backlog.
        self._go_online()
        self._make_due()
        ctl.tick(index_busy=False)
        st = git_sync.read_status(self.vault)
        self.assertEqual(st["state"], "ok", msg=st)
        self.assertIsNone(st["error"])
        self.assertIsNone(st["offline_since"])
        self.assertEqual(st["retry_count"], 0)
        self.assertIsNone(st["next_retry_at"])
        self.assertEqual(self._remote_head(), self._local_head())

    def test_retry_not_due_does_nothing(self):
        self._go_offline()
        (self.vault / "note.md").write_text("# N\n\noffline 1\n", encoding="utf-8")
        git_sync.commit_and_push(self.vault)
        self._go_online()
        ctl = git_sync.VaultSyncController(self.vault, verbose=False)
        ctl.tick(index_busy=False)  # next_retry_at is ~60s out
        self.assertEqual(git_sync.read_status(self.vault)["state"], "offline")
        self.assertNotEqual(self._remote_head(), self._local_head())

    def test_idle_pull_pushes_lost_backlog(self):
        # A commit that never got pushed (e.g. push lost before this fix) is
        # retried by the next successful idle pull.
        (self.vault / "note.md").write_text("# N\n\nunpushed\n", encoding="utf-8")
        git_sync.commit_and_push(self.vault, push=False)
        self.assertEqual(git_sync.commits_ahead_of_upstream(self.vault), 1)
        ctl = git_sync.VaultSyncController(self.vault, verbose=False)
        ctl._last_pull_at = -1e9  # noqa: SLF001 — test hook: interval elapsed
        ctl.tick(index_busy=False)
        self.assertEqual(self._remote_head(), self._local_head())
        self.assertEqual(git_sync.read_status(self.vault)["state"], "ok")

    def test_legacy_network_block_self_heals(self):
        (self.vault / "note.md").write_text("# N\n\nstuck\n", encoding="utf-8")
        git_sync.commit_and_push(self.vault, push=False)
        git_sync._block(
            self.vault,
            "fatal: unable to access 'https://github.com/x/y.git/': Could not resolve host: github.com",
        )

        ctl = git_sync.VaultSyncController(self.vault, verbose=False)
        ctl.tick(index_busy=False)
        st = git_sync.read_status(self.vault)
        self.assertEqual(st["state"], "ok", msg=st)
        self.assertEqual(self._remote_head(), self._local_head())

        # Same heal against a remote that is still down keeps the outage start.
        git_sync._block(self.vault, "fatal: Failed to connect to github.com port 443")
        blocked_at = git_sync.read_status(self.vault)["blocked_at"]
        self._go_offline()
        ctl.tick(index_busy=False)
        st = git_sync.read_status(self.vault)
        self.assertEqual(st["state"], "offline")
        self.assertEqual(st["offline_since"], blocked_at)
        self.assertEqual(st["retry_count"], 1)

    def test_real_conflict_block_stays_blocked(self):
        err = (
            "error: The following untracked working tree files would be overwritten by merge:\n"
            "\tnote2.md\nPlease move or remove them before you merge.\nAborting"
        )
        git_sync._block(self.vault, err)
        ctl = git_sync.VaultSyncController(self.vault, verbose=False)
        ctl.tick(index_busy=False)
        st = git_sync.read_status(self.vault)
        self.assertEqual(st["state"], "blocked")
        self.assertEqual(st["error"], err)

    def test_non_ff_divergence_still_blocks(self):
        clone = self.tmp / "other"
        _git(self.tmp, "clone", str(self.bare), str(clone))
        _git(clone, "config", "user.email", "test@example.com")
        _git(clone, "config", "user.name", "Test")
        (clone / "note.md").write_text("# remote\n", encoding="utf-8")
        _git(clone, "commit", "-am", "remote change")
        _git(clone, "push", "origin", "HEAD:main")

        (self.vault / "note.md").write_text("# local\n", encoding="utf-8")
        out = git_sync.commit_and_push(self.vault)
        self.assertFalse(out["ok"], msg=out)
        self.assertEqual(out["error"], "push_failed")
        self.assertEqual(git_sync.read_status(self.vault)["state"], "blocked")

    def test_network_timeout_goes_offline(self):
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if "push" in cmd:
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
            return real_run(cmd, *args, **kwargs)

        (self.vault / "note.md").write_text("# N\n\nslow\n", encoding="utf-8")
        with unittest.mock.patch.object(git_sync.subprocess, "run", side_effect=fake_run):
            out = git_sync.commit_and_push(self.vault)
        self.assertEqual(out["error"], "offline", msg=out)
        st = git_sync.read_status(self.vault)
        self.assertEqual(st["state"], "offline")
        self.assertIn("timed out", st["error"])

    def test_network_ops_disable_prompts_and_set_low_speed_abort(self):
        seen: dict[str, object] = {}
        real_run = subprocess.run

        def spy(cmd, *args, **kwargs):
            if "push" in cmd:
                seen["cmd"] = cmd
                seen["env"] = kwargs.get("env")
            return real_run(cmd, *args, **kwargs)

        with unittest.mock.patch.object(git_sync.subprocess, "run", side_effect=spy):
            git_sync._run_git_network(self.vault, "push")
        self.assertIn("http.lowSpeedLimit=1000", seen["cmd"])
        self.assertIn("http.lowSpeedTime=20", seen["cmd"])
        self.assertEqual(seen["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_backoff_doubles_and_caps(self):
        self.assertEqual(git_sync.offline_backoff_seconds(0, 900), 60)
        self.assertEqual(git_sync.offline_backoff_seconds(1, 900), 120)
        self.assertEqual(git_sync.offline_backoff_seconds(3, 900), 480)
        self.assertEqual(git_sync.offline_backoff_seconds(4, 900), 900)
        self.assertEqual(git_sync.offline_backoff_seconds(10_000, 900), 900)

        # Contract pull interval (60s in this fixture) caps the stored schedule.
        self._go_offline()
        for _ in range(4):
            git_sync.pull_ff_only(self.vault)
        st = git_sync.read_status(self.vault)
        self.assertEqual(st["retry_count"], 4)
        due = datetime.fromisoformat(st["next_retry_at"])
        updated = datetime.fromisoformat(st["updated_at"])
        self.assertLessEqual((due - updated).total_seconds(), 61)


class GitSyncReadOnlyVaultTest(unittest.TestCase):
    """A read-only mount (e.g. an ingested foreign OKF bundle) must not get
    unattended git-sync automation either — not just MCP writes.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo-gsync-ro-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "note.md").write_text("# N\n\nv1\n", encoding="utf-8")
        _git(self.vault, "init", "-b", "main")
        _git(self.vault, "config", "user.email", "test@example.com")
        _git(self.vault, "config", "user.name", "Test")
        _git(self.vault, "add", "note.md")
        _git(self.vault, "commit", "-m", "initial")
        (self.vault / "system" / "contracts").mkdir(parents=True)
        (self.vault / "system" / "contracts" / "usage-contract.schema.yaml").write_text(
            "usage_contract_version: '0.1'\nvault_id: ro_sync\npurpose: read-only sync test\n",
            encoding="utf-8",
        )
        self.sentinel = self.tmp / "notified.log"
        _write_contract(
            self.vault,
            enabled=True,
            extra=(
                "  on_block_command: 'printf \"%s\\n\" \"$APO_SYNC_ERROR\" "
                f">> {self.sentinel}'\n"
            ),
        )
        self.vaults_file = self.tmp / "vaults.json"
        self.vaults_file.write_text(
            json.dumps(
                {
                    "default": "ro_sync",
                    "vaults": {
                        "ro_sync": {
                            "root": str(self.vault),
                            "index": str(self.tmp / "ro_sync.db"),
                            "read_only": True,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self._prev_apo_vaults = os.environ.get("APO_VAULTS")
        os.environ["APO_VAULTS"] = str(self.vaults_file)

    def tearDown(self):
        if self._prev_apo_vaults is None:
            os.environ.pop("APO_VAULTS", None)
        else:
            os.environ["APO_VAULTS"] = self._prev_apo_vaults
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sync_enabled_false_despite_contract_enabled_true(self):
        # The contract says `sync.enabled: true`; the registry's read_only
        # flag must override that for this root.
        self.assertTrue(git_sync.sync_settings(self.vault).enabled)
        self.assertFalse(git_sync.sync_enabled(self.vault))

    def test_controller_never_commits(self):
        ctl = git_sync.VaultSyncController(self.vault, verbose=False)
        (self.vault / "note.md").write_text("# N\n\ndirty\n", encoding="utf-8")
        ctl.note_apo_writes()
        # Pre-fix this would arm the debounce (contract says enabled: true);
        # the read_only flag must stop it before that.
        self.assertFalse(ctl.pending_commit())

        # Force a commit "due" directly (same hook `test_controller_debounce_commit`
        # uses on a writable vault, where it does trigger a commit) — on a
        # read_only vault, tick() must still refuse to act on it.
        ctl._commit_due_at = 0.0  # noqa: SLF001 — test hook
        ctl.tick(index_busy=False)

        log = _git(self.vault, "log", "-1", "--pretty=%s")
        self.assertEqual(log.stdout.strip(), "initial")
        self.assertFalse(self.sentinel.exists())


if __name__ == "__main__":
    unittest.main()
