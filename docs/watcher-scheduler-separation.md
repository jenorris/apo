# Watcher scheduler separation — design note (proposal, not implemented)

**Status:** proposal for review. No code changed as part of this note — the watcher is a
currently-running production daemon (see `docs/index-concurrency.md`: it's the **sole**
`index.db` writer) and this is exactly the kind of change that shouldn't land without Jeremy
picking the direction first.

## Problem

`_watch_one`'s loop in `engine/src/apo_engine/watch.py` has one stated job — per
`docs/index-concurrency.md` — be the sole `index.db` writer for its vault, and stay responsive
enough that the read-after-write scheduling bound (`APO_WATCH_DEBOUNCE`, default 2s) holds.

That loop also runs three pieces of unrelated business logic on every cycle:

| Call | Line | What it does |
|------|------|---------------|
| `sync_ctl.tick(...)` | `watch.py:461` | Git auto-commit/push (`git_sync.VaultSyncController`) |
| `merge_ctl.tick(...)` | `watch.py:467` | Optima-vault-specific schedule merge (`optima_merge.VaultMergeController`) |
| `vault_project.maybe_reproject(...)` | `watch.py:422`, `watch.py:436` | Desk/contracts reprojection (called twice per cycle: once on contract-relevant file changes, once unconditionally as "desk-poll") |

Each is wrapped in its own `try/except Exception`, but that only guards against a call that
*raises*. None of the three carries a timeout or runs off the main loop thread, so a call that
*hangs* — blocks without raising — stalls the whole cycle: no more debounced index writes, no
more deferred-queue consumption, no more `wake-*` responsiveness, for as long as the hang lasts.
`except Exception` cannot catch that; the loop simply never gets back to `event_queue.get(timeout=...)`.

## Existing partial mitigation (and its limit)

`git_sync._run_git` already wraps every git subprocess call in `subprocess.run(..., timeout=_GIT_TIMEOUT_S)`
(`git_sync.py:33`, 120s). A hung `git push` over a dead network link raises `TimeoutExpired`
after 120s, which the watcher's `except Exception as sync_err` at `watch.py:462` does catch —
so the **subprocess boundary** is bounded today, just at a fairly long ceiling (120s of a stalled
index-write loop per hang, per vault) and only for git_sync's own subprocess calls.

Nothing else is bounded that way:

- `merge_ctl.tick()` → `optima_merge.run_merge` is pure local file I/O (read/write YAML) —
  no subprocess timeout to fall back on. A slow/blocked read (e.g. a flaky network mount) hangs
  with no ceiling at all.
- `vault_project.maybe_reproject()` does mtime/signature comparisons and, on drift, a project
  render — also local I/O, no timeout.

## The cross-vault blast radius is uneven

In multi-vault mode (`run_watch`, `watch.py:125`), each vault gets **its own thread**
(`_watch_one` per binding). `sync_ctl` and `merge_ctl` are instantiated per `_watch_one` call
(`watch.py:294`, `watch.py:297`) — vault-scoped, so a hang there stalls only that one vault's
index writes. That's already bad (it *is* that vault's sole writer), but it's contained.

`vault_project.maybe_reproject`, however, guards its work with a **module-level**
`_reproject_lock` (`vault_project.py:1257`) shared by every vault's watcher thread. A slow or
hung reproject in one vault's call holds that lock; every other vault's thread calling
`maybe_reproject()` on its own next cycle blocks waiting for it. One vault's desk-poll hang can
therefore stall **every other vault's** index-write responsiveness too, not just its own —
the one piece of shared state most likely to create exactly the kind of hang this note is about.

## Direction to decide between

Not prescribing an implementation — these are the shapes worth choosing between, roughly
cheapest-to-most-separated:

1. **Bounded background thread per hook, non-blocking loop.** Each of the three calls runs on
   its own worker thread with a wall-clock budget; the main loop starts the call (if not already
   running) and moves on immediately rather than waiting on it — the same shape the debounce
   timer already uses (schedule, check readiness, don't block). A thread that blows its budget
   gets flagged (see below) rather than joined/killed — Python threads can't be force-killed
   cleanly, so this accepts a leaked thread as the cost of never blocking the index writer.
2. **Same, but with an actual watchdog timeout** that can at least interrupt cooperative work
   (e.g. checking a `threading.Event` between steps inside `run_merge`/`maybe_reproject`) so a
   flagged hang can be asked to stop rather than only detected.
3. **Move the three hooks out of the watcher process entirely**, coordinating only through
   files/queues the way MCP → watcher already does for index writes (per
   `docs/index-concurrency.md`'s `deferred-*`/`wake-*` convention). Fully decouples "sole index
   writer" from "peripheral background jobs," at the cost of a new process/scheduler to run and
   monitor.

Option 1 is the smallest change and fixes the actual problem statement (loop must never block on
these three calls); option 3 is the "do it right" answer if the peripheral jobs keep growing.

## Surfacing health

Whichever direction, the resulting "last run at / last duration / currently hung" state per hook
should be surfaced somewhere an operator can see it — the natural existing surface is the
watcher status already exposed via `apo_admin(memory_status)` / `ops.watcher_status()`
(`docs/index-concurrency.md`'s `index_visibility()` neighbor). Whatever health/introspection
capability the `index-health-doctor` work is adding is the right place to add this, once that
work is confirmed done in that worktree.

**Status (implemented):** this section, and the logging fix from the companion review, landed —
`engine/src/apo_engine/watch_health.py` tracks `last_tick_at` / `last_tick_seconds` / `ok` / `error`
per hook (`git_sync`, `optima_merge`, `desk_reproject`) in a small per-vault
`.apo/watch-hooks.json`, read by `core.index_health()` (`hooks` field + `hook_error:*` /
`hook_stale:*` flags) and printed by `apo-engine doctor`. The thread-per-hook scheduler above
(direction 1–3) was **not** built — reviewed and rejected as overbuilt for a solo-maintainer loop
whose peripheral calls are already either subprocess-timeout-bounded (`git_sync`) or cheap local
I/O; a stall is now visible instead of invisible, which was the actual ask.
