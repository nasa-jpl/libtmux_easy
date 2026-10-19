# Architecture

## Modules

| module | deps | role |
|---|---|---|
| `tmux_lib.py` | libtmux, psutil | `TmuxSession`: windows, panes, modes, status, stopping, dashboard |
| `status.py` | - | `ExitStatus`, `PaneStatus` |
| `watcher.py` | psutil | `_PaneWatcher`: one thread per session; pidfds plus the wrap socket |
| `proc.py` | psutil | process discovery under a pane |
| `wrap.py` | stdlib | `lte-run` |
| `dash/` | stdlib | `options` (state in tmux options), `window` (`open_window`), `viewport` (`Viewport`), `control` (`Control`, `read_status`), `cli` (`lte-dash`) |
| `dash/app.py` | Textual | the sidebar UI |
| `dash/keys.py` | stdlib | `lte-key`: borrow a prefix key |
| `dash/homelog.py` | - | stdout/logging tee behind `tee_home()` |
| `launch.py` | stdlib | `lte-session` |
| `close.py` | stdlib | `lte-close`: graceful close, close actions |
| `starter.py` | stdlib | `lte-defaults`: starter tmux settings for a server with no config |
| `output.py` | stdlib | output capture (`pipe-pane` files), `OutputReader` (tails them), `Capture`, watches |
| `testing.py` | pytest | pytest plugin |
| `_tmux.py` | stdlib | `tmux()` / `option()` for the stdlib modules |

- **Stdlib-only commands.** `lte-run` runs once per pane: importing libtmux
  and psutil cost ~4x the startup and +7 MB per pane. `__init__` loads names
  lazily for the same reason.
- **Textual is optional** (`[dash]` extra). The `.deb` leaves out `lte-dash`:
  apt's Textual is too old.

## Sessions

| constructor | owns | `release()` | `release(keep_windows=True)` |
|---|---|---|---|
| `create(name)` | the whole session (replaces one of that name) | kill it | stop tracking, leave it up |
| `current()` | only the windows it makes | close those windows | keep them, renamed `<label>/<window>` |

- `current()` adopts the session the process runs in (e.g. under
  `lte-session`): no unnamed first window, the dashboard is reused.
- `create()` keeps the initial window (by id) and reuses it for the first
  `new_window()`, so a dashboard window opened first is never renamed.
- `kill_session()` only sends SIGHUP. Call `stop_all()` first for a clean
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
- `Session.kill` (libtmux >= 0.30) is preferred over `kill_session`; `kill()`
  picks whichever exists.

## Environment

`env=` at three levels: `create()` / `copy_environment()` (session),
`new_window()`, `new_pane()`; merged in that order in `_claim_pane()`.

- Created session: `set-environment`, so panes made by hand get it too.
- Every library pane also gets the merged env with `-e` (split, or
  `respawn-pane -e`). A window's reused first pane already runs a shell, so
  it is respawned when there is any env. An exec pane's respawn reuses it.
- `_tmux.SKIP_ENV` is never copied: tmux sets `TMUX*`/`TERM` per pane, and a
  copied `LTE_DIR` would send `lte-run` to another session's socket.

## State in tmux options

Anything a second process needs (sidebar, `lte-run`, a restarted script)
lives in tmux options, so they all agree without extra IPC:

| scope | options |
|---|---|
| session | `@lte_dir` (socket dir), `@lte_status` (status file), `@lte_run_dir`, `@lte_home_status`, `@lte_dash_window`, `@lte_dash_sidebar` |
| window | `@lte_dash_home/view/from/home_cmd/width/home_kind/initial` |
| pane | `@lte_pipe_file`, `@lte_pipe_from`, `@lte_pipe_id`, `@lte_pipe_tee`: where its output goes ([output-match.md](output-match.md)) |
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
- `TmuxSession` is ~1700 lines; wrap and dashboard methods could be mixins.
