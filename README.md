# libtmux-easy

Configure tmux sessions with Python, and supervise what runs in them: exact
exit status, exit events, a live dashboard, and pytest fixtures that launch
the processes a test needs.

## Quick start

```python
from libtmux_easy import TmuxSession

session = TmuxSession.create("my-session")
session.new_window("work")
session.new_pane("editor", "nvim")
session.new_pane("server", "python3 -m http.server", mode="wrap")

session.new_window("monitoring", layout="main-horizontal")
session.new_pane("logs", "journalctl -f")

session.attach()
```

Panes are named `window:pane`; anything that takes a pane takes that name,
its id (`%3`) or the `Pane`.

## Installation

| platform | method | installs |
|---|---|---|
| Ubuntu / Debian | `.deb` (below) | library for the system Python, `lte-run`, `lte-session`, `lte-key` |
| a venv | `pip install libtmux-easy` (`[dash]` for the dashboard) | library and commands, in the venv |
| commands on PATH | `pipx install 'libtmux-easy[dash]'` | the commands, with Textual |
| a checkout | `PYTHONPATH=src` | everything |

```bash
sudo apt install build-essential debhelper dh-python python3-all \
                 python3-setuptools pybuild-plugin-pyproject
dpkg-buildpackage -us -uc -b && sudo apt install ../python3-libtmux-easy_0.1.0_all.deb
```

- The `.deb` leaves out `lte-dash`: apt's Textual is too old (use pipx).
- A venv sees the `.deb` only if made with `--system-site-packages`.
- Wrap-mode panes type the bare `lte-run`: it must be on each pane's PATH
  (`send_command_before("source .venv/bin/activate")`, or pipx).
- Version bumps go in both `pyproject.toml` and `debian/changelog`.

## API

| method | |
|---|---|
| `TmuxSession.create(name, status_file=None, env=None)` | new session (replaces one of that name) |
| `TmuxSession.current(env=None)` | adopt the session this process runs in |
| `new_window(name, layout="tiled", env=None)` | new window; the next panes go in it |
| `new_pane(name, cmd, mode="shell", on_exit=None, env=None, tee=None)` | new pane running `cmd`; `tee=` (wrap mode) also writes its output to a file |
| `send_keys(cmd, target_pane=None)`, `send_command_before(cmd, window=None)` | type into a pane; set up every new pane |
| `copy_environment(env=None)` | add `env` (default `os.environ`) to the session's; see Environment |
| `select_window()`, `select_pane()`, `attach()` | |
| `is_running(pane)`, `exit_status(pane)` | liveness; `ExitStatus` (`.code`, `.signal`, `.ok`, `.known`) |
| `watch_pane(pane, on_exit)`, `wait_for_exit(pane, timeout)` | exit events |
| `stop_pane(pane)`, `stop_all()` | SIGINT, then SIGTERM, then SIGKILL; `stop_all()` in parallel |
| `respawn_pane(pane, cmd=None)` | rerun in place, any mode |
| `pane_status(pane)`, `status()`, `print_status()` | `PaneStatus`: `.state` running / stopped / ok / failed / unknown |
| `dashboard()`, `dashboard_pane()`, `close_dashboard()` | see Dashboard |
| `kill_session()`, `release(keep_windows=False)` | end, or let go and leave windows up |

Status is also on each pane's border title (`server [exited 3]`), and with
`status_file=` in a JSON file rewritten on every change.

Kill the session only after `stop_all()`: `kill_session()` alone just sends
SIGHUP.

## Environment

Panes otherwise get the tmux **server's** environment, which may predate an
activated venv or a sourced workspace. Pass `env=` (`os.environ`, or any dict)
at any level; later levels win:

| level | how | reaches |
|---|---|---|
| session | `create(env=)`, `copy_environment(env)` | every pane, including ones made by hand (the session's tmux environment) |
| window | `new_window(name, env=)` | panes `new_pane()` makes in it (tmux has no window environment) |
| pane | `new_pane(name, cmd, env=)` | that pane |

- Added to the server's environment, not a replacement for it.
- Never copied: `TMUX`, `TMUX_PANE`, `TERM`, `TERM_PROGRAM*`, `COLUMNS`,
  `LINES` (tmux sets them per pane) and `LTE_DIR` (it would point `lte-run`
  at another session).
- Under `current()`, only this script's panes get it.

## Pane modes

| `mode=` | how | exit status | interactive |
|---|---|---|---|
| `"shell"` (default) | typed into bash | read from `$?`: a signal death is *inferred* from 128+N | yes |
| `"exec"` | the command is the pane process | from tmux, exact, but tmux sometimes loses it | no |
| `"wrap"` | typed as `lte-run <cmd>` | from `lte-run`, exact | yes |

Use **wrap** when the status matters. Rerun with `respawn_pane()`; plain
`send_keys()` restarts the command but the library keeps reporting the old
run (wrap mode tracks any rerun, even typed by hand).

**`lte-run [--tee FILE [-a] [--raw]] [--] COMMAND`** works in any pane of a
libtmux_easy session: it reports the command's pid and exact status over the
session's socket. Elsewhere it just runs the command, same output and status.
Ctrl-C, Ctrl-Z / `fg` and stdin work as usual. Shell operators typed by hand
(`lte-run a && b`) supervise only `a`; `new_pane()` wraps the whole command in
`bash -c`. Linux only.

| tee | |
|---|---|
| `--tee FILE` | also copy the output to FILE, like `tee` (stdout and stderr, one stream); `-a` appends |
| `LTE_RUN_TEE=FILE` | the same, from the environment (`--tee` wins); not passed on to the command |
| in a tmux pane | the command keeps its terminal: tmux's `pipe-pane` copies the output |
| elsewhere | the command writes to a pipe, as with `cmd 2>&1 \| tee FILE` |
| file contents | plain text: escapes dropped, `\r` redraws collapsed (`--raw` keeps them); complete when the exit is reported |

**Exec mode caveat:** tmux occasionally never reaps the pane process, and
the status is lost (reproduced with raw tmux). `exit_status()` then returns
unknown and logs a warning. See `docs/KNOWN_ISSUES.md`.

## Dashboard

```python
session.dashboard()                                       # window "dash", always first
session.dashboard_pane("htop", title="load", size="30%")  # optional, beside the viewport
```

Left: the sidebar (`lte-dash`), with running / stopped / failed counts and
every pane by window. Right: the selected pane, swapped in with
`tmux swap-pane`. It is the real pane, fully interactive, and every window
keeps its layout. `home` shows the script's stdout, stderr and logging.

| key | action |
|---|---|
| `Up` / `Down`, `Enter` | browse (the viewport follows); focus the pane |
| `g` | go to the pane in its own window; `prefix g` comes back |
| `x` / `r` / `c` | stop / rerun the pane / copy its command |
| `X` / `Q` | stop all / close the session with `lte-close` (press twice) |
| `[` / `]`, `?`, `q` | resize, help, close |

- Colors: green running or exit 0, red failed, yellow stopped (by `x`, `X`,
  `stop_pane()`), magenta unknown.
- Actions reach the script over its socket: they work while it runs.
- The pane on view is swapped back into its own window when you select
  another window, so tmux navigation shows every window whole. Previews in
  `prefix w` are taken before that: the viewed pane's window shows `home`
  in its slot.
- `prefix g` is **borrowed** while the sidebar runs (`lte-key`) and returned
  after; nothing goes in `~/.tmux.conf`. `dashboard(key=...)` picks another.
- Turns on the session's `mouse` (`mouse=False` to skip).
- `dashboard(initial=pane)` selects and shows that pane first, instead of `home`.
- `dashboard(width=)`: `"auto"` (default) sizes the sidebar once, at start,
  to the longest pane name plus the longest status (at least 36 columns, at
  most half the window); or columns, or `"25%"`. `[` / `]` resize after.
- Needs Textual: `pip install textual` in the venv, or `pipx install 'libtmux-easy[dash]'`.
  Plain `pipx install .` (no extras) still installs `lte-dash` on PATH, but it
  exits with the install hint instead of opening -- Textual is an extra, not
  a base dependency, because the same `pyproject.toml` drives the `.deb`
  build, and apt's Textual is too old/absent (see `debian/rules`).
- Without a `TmuxSession`: `lte-dash --open SESSION --status FILE --ctl SOCKET
  --home-log FILE` (or `dash.open_window()`), then keep the status file
  current and answer the socket's `stop` / `respawn` / `stop_all` / `shutdown`.

`lte-key borrow KEY OPTION -- COMMAND` / `lte-key return KEY`: bind
`prefix KEY` to COMMAND where session option OPTION is set, to its previous
binding elsewhere. Counted, so borrowers can share a key.

## Testing with pytest

```python
# conftest.py
pytest_plugins = ["libtmux_easy.testing"]

# test_web.py
PROCESSES = {"servers:web": "python3 -m http.server 8000"}

def test_serves(processes):
    ...                              # processes.output("servers:web"), processes.check()
```

`processes` (module scope) starts `PROCESSES`. A test fails if one exits
before teardown, even if the test never noticed. Teardown stops them and
errors unless each exited 0 on SIGINT.

| `--lte-mode` | processes | after the run |
|---|---|---|
| `shell` | subprocesses; output in the terminal, prefixed `[name]` | stopped |
| `tmux` | wrap panes in a detached session; only test output in the terminal | closed on success, kept on failure (`--lte-keep`) |
| `dash` | under `lte-session`: in its session and dashboard | kept with `lte-session --keep` |

Default `auto`: dash under lte-session, else tmux, else shell. In every
mode processes get pytest's environment (tmux modes pass it with `env=`). Override the
`lte_pane_setup` fixture to run a setup command first (e.g. source an
environment), and `lte_output_checks` to fail on deaths only the output
shows (e.g. a supervisor's children). `tmux_session` gives a test a
`TmuxSession` of its own.

## Closing a session

tmux has no hook before a session closes, so a graceful close is a command:
`lte-close` opens a `close` window and shows it run the close actions, stop
every pane (Ctrl-C, then SIGTERM, SIGKILL), and kill the session.

```python
sess.on_close("./cleanup.sh")                    # shell: runs even if the script died
sess.on_close(lambda s: save_logs(s), when="before")  # Python: while the script runs
sess.bind_close_key("C-x")                        # prefix C-x closes this session gracefully
sess.close()                                      # or do it all from the script
```

Without a script: `lte-close add -- ./cleanup.sh`, and in `~/.tmux.conf`
`bind-key C-x run-shell -b "lte-close -t '#{session_id}'"`. The dashboard's
`Q` `Q` runs `lte-close` too. See `docs/session-close.md`.

## lte-session

```bash
lte-session -- pytest -s tests/
```

- The command runs in the dashboard's home pane, a real terminal: `print()`,
  `input()`, pdb and Ctrl-C work. Panes the tests start appear in the sidebar.
- It gets your shell's environment and cwd, not the tmux server's.
- When the command ends (or on detach, `prefix d`), its scrollback is
  printed in your terminal, and `lte-session` exits with the command's status. Still running: the output
  keeps streaming here. No TTY (CI): streams through. Inside tmux: switches
  your client instead of nesting.

| flag | |
|---|---|
| `--name NAME` | session name (default `lte_<command>_<pid>`) |
| `--keep` | stay after the command ends (to inspect panes); leave the session up |
| `--width N` | sidebar columns, `N%`, or `auto` (default; no panes yet at start, so its minimum) |
| `--no-dash` | home pane only (also the fallback without Textual) |

## Tours

`demos/interactive/`: hands-on tours that narrate each step and wait for you. Start with
`demos/interactive/01_session.py`; tours 2-5 (exit status, process control, dashboard,
monitoring) run inside the dashboard. Plus the pytest plugin in all three modes.

## Design docs

`docs/`: architecture, exit status, wrap protocol, dashboard, lte-session,
the pytest plugin, and [known issues](docs/KNOWN_ISSUES.md).

## Compatibility

Linux. Tested with libtmux 0.10.1 to 0.62, tmux 3.3a and 3.4.

## License

Apache-2.0
