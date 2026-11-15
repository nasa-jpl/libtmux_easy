# Architecture

## Modules

Two packages: `lte_run` (stdlib only, never imports `libtmux_easy`) and
`libtmux_easy` (the library), which imports from it.

| module | deps | role |
|---|---|---|
| `tmux_lib.py` | libtmux, psutil | `TmuxSession`: windows, panes, modes, status, stopping, dashboard |
| `status.py` | - | `ExitStatus`, `PaneStatus` |
| `watcher.py` | psutil | `_PaneWatcher`: one thread per session; pidfds plus the wrap socket |
| `lte_run/proc.py` | stdlib; psutil on use | processes: `/proc` readers for the commands, psutil ones for the library |
| `lte_run/wrap.py` | stdlib | `lte-run`; pipe guards and plain text, below |
| `lte_dash/` | stdlib | `options` (state in tmux options), `window` (`open_window`), `viewport` (`Viewport`), `control` (`Control`, `read_status`), `winhook` (window-change hook), `cli` (`lte-dash`) |
| `lte_dash/app.py` | Textual | the sidebar UI |
| `lte_dash/keys.py` | stdlib | `lte-key`: borrow a prefix key |
| `lte_dash/homelog.py` | - | stdout/logging copy behind `log_home()` |
| `lte_session.py` | stdlib | `lte-session` |
| `close.py` | stdlib | `lte-close`: graceful close, close actions |
| `lte_defaults.py` | stdlib | `lte-defaults`: starter tmux settings for a server with no config |
| `output.py` | stdlib | output capture (`pipe-pane` files), `OutputReader` (tails them), `Capture`, watches |
| `processes.py` | the library | `ProcessGroup`: a process group, its backends (subprocess, tmux, adopted), health; no pytest |
| `testing.py` | pytest | pytest plugin: `Processes` with a test owner, on top of `processes.py` |
| `tmuxcmd.py` | stdlib | `tmux()` / `option()`: raw tmux commands for the stdlib modules |

- **lte-run is its own package.** It runs once per pane, for as long as the
  command: importing `libtmux_easy` (libtmux, psutil) would cost each about 3x
  the startup and +8 MB. So `lte_run` holds it and the stdlib helpers it
  needs, and `libtmux_easy/__init__.py` is plain imports.
- **Stdlib-only commands.** `lte-session`, `lte-close`, `lte-key`,
  `lte-defaults` and `lte-dash` send raw tmux commands through `tmuxcmd.py`;
  they use none of libtmux's objects.
- **Commands run as modules.** Not installed, a command runs as
  `env PYTHONPATH=<src> python -m libtmux_easy.<module>` (`close.self_command()`),
  never as a script, so modules import each other normally. lte-run is the
  exception: wrap mode types bare `lte-run`, found on the pane's PATH.
- **Textual is optional** (`[dash]` extra). apt's is too old, so `lte-dash` is
  its own `.deb`, `libtmux-easy-dash`, with Textual bundled in a private directory.

### Shared helpers

One copy each. The commands import them, so their modules stay light:
`lte_run/proc.py` imports psutil inside its psutil functions only
(`test_wrapper_import_stays_light`).

| helper | in | used by |
|---|---|---|
| `tmux()`, `option()`, `pane_env()`, `SKIP_ENV` | `tmuxcmd.py` | dash, keys, launch, close, starter, the library |
| `proc_stat()`, `descendants()`, `alive()`, `pane_pids()` (`/proc`) | `lte_run/proc.py` | lte-run (signals), lte-close (what a pane runs) |
| `proc_alive()`, `pane_process()`, `pane_child_processes()` (psutil) | `lte_run/proc.py` | the library, the watcher |
| `guarded_pipe()`, `guarded_unpipe()`, `tmux_literal()` | `lte_run/wrap.py` | lte-run `--tee`, the library's captures |
| `plain_line()`, `plain_text()` | `lte_run/wrap.py` | `--tee`, output matching, the pytest plugin |
| `file_size()`, `typed_echo()`, `plain_lines()` | `output.py` | the reader, searches |

### `tmux_lib.py` sections

| section | |
|---|---|
| module functions | server, session creation, `_pane_cmd()` / `_type()` (always `-t`) |
| constructors, windows, panes | `create()`, `current()`, `new_window()`, `new_pane()`, `_claim_pane()` |
| status | `is_running()`, `exit_status()`, `stop_pane()`, `respawn_pane()` |
| exit events, wrap mode | the watcher's callbacks: `_on_target_exit()`, `_on_wrap_message()` |
| output | `capture()`, `watch_output()`, pipes (`_pipe_many()`), the reader |
| closing | `on_close()`, `close()`, `kill()`, `release()` |
| dashboard, status surface | `dashboard()`, `log_home()`, `status()`, the status file, titles |

## Sessions

| constructor | owns | `release()` | `release(keep_windows=True)` |
|---|---|---|---|
| `create(name)` | the whole session (replaces one of that name) | kill it | stop tracking, leave it up |
| `current()` | only the windows it makes | close those windows | keep them, renamed `<label>/<window>` |

- `current()` adopts the session the process runs in (e.g. under
  `lte-session`): no unnamed first window, the dashboard is reused.
- `create()` keeps the initial window (by id) and reuses it for the first
  `new_window()`, so a dashboard window opened first is never renamed.
- A window starts with a placeholder pane (`sleep`, silent). The first
  `new_pane()` splits off it, then kills it: every pane is named and made
  the same way.
- `kill()` only sends SIGHUP. Call `stop_all()` first for a clean
  shutdown.

## Talking to tmux

*Verified* rules every module follows:

- **Query with `display-message -p FORMAT`**, not libtmux attributes: those
  are cached and version-dependent. Test `#{pane_dead}` against `"1"`; a
  failed query returns `""`.
- **Always pass `-t`.** libtmux 0.30 `Pane.cmd()` adds its own `-t` only when
  no argument contains `-t`, so text like `--topic` breaks it. Use
  `_pane_cmd()` / `_type()`.
- **Pass title text as a list argument**, never through `shlex.split`.
- `#{...}` expands in `run-shell` / `if-shell -F` strings, not in `-t` targets.
- `TmuxSession.kill()` ends a session; libtmux's old `kill_session()` raises
  (it would skip the cleanup). Needs libtmux 0.30 or later.

## Environment

Exact per pane: `os.environ`, then `env=` at session (`create()` /
`copy_environment()`), window and pane level (`_pane_environment()`).
Each split or respawn sets it on the session, splits and restores, in one
`source-file` call (`_spawn()`); shell panes run the user's rc, then
restore the env (`lte_rc.bash`). See [environment.md](environment.md).

- Created session: `copy_environment()` also sets the session's tmux
  environment, so panes made by hand get it. Adopted: left alone.
- `tmux_lib.SKIP_ENV` is never copied: tmux sets `TMUX*`/`TERM` per pane, and a
  copied `LTE_DIR` would send `lte-run` to another session's socket.

## State in tmux options

Anything a second process needs (sidebar, `lte-run`, a restarted script)
lives in tmux options, so they all agree without extra IPC:

| scope | options |
|---|---|
| session | `@lte_dir` (socket dir), `@lte_status` (status file), `@lte_run_dir`, `@lte_home_status`, `@lte_dash_window`, `@lte_dash_sidebar` |
| window | `@lte_dash_home/view/from/home_cmd/width/home_kind/initial` |
| pane | `@lte_pipe_file`, `@lte_pipe_from`, `@lte_pipe_id`, `@lte_pipe_tee`, `@lte_pipe_whole`: where its output goes ([output.md](output.md)) |
| global | `@lte_starter`, `@lte_key_dash`, `@lte_key_close` (the user's), `@lte_key_*_users/_saved` (lte-key) |
| session, window | `@lte_pipe`, `@lte_pipe_cmd`: capture template for new panes; session `@lte_pipe_next*`: the next library pane's file |

## Threads

| thread | per | wakes on |
|---|---|---|
| `_PaneWatcher` | session | pidfds, the wrap socket |
| `OutputReader` | session, once something is watched | inotify writes to watched files (0.1 s minimum gap), a 1 s `list-panes` net; blocked with no watches |
| `Tee._copy` | `lte-run --tee` | its FIFO |

## Design smells

- `create()` / `current()` reassign a libtmux `Session`'s `__class__`.
  Fragile across libtmux upgrades.
- Global mutable `_server` (`set_server()`): one server per process.
- `TmuxSession` is ~2200 lines; the output, wrap and dashboard sections
  could be mixins.
