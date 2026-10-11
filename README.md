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
| `TmuxSession.create(name, status_file=None)` | new session (replaces one of that name) |
| `TmuxSession.current()` | adopt the session this process runs in |
| `new_window(name, layout="tiled")` | new window; the next panes go in it |
| `new_pane(name, cmd, mode="shell", on_exit=None)` | new pane running `cmd` |
| `send_keys(cmd, target_pane=None)`, `send_command_before(cmd, window=None)` | type into a pane; set up every new pane |
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

## Pane modes

| `mode=` | how | exit status | interactive |
|---|---|---|---|
| `"shell"` (default) | typed into bash | read from `$?`: a signal death is *inferred* from 128+N | yes |
| `"exec"` | the command is the pane process | from tmux, exact, but tmux sometimes loses it | no |
| `"wrap"` | typed as `lte-run <cmd>` | from `lte-run`, exact | yes |

Use **wrap** when the status matters. Rerun with `respawn_pane()`; plain
`send_keys()` restarts the command but the library keeps reporting the old
run (wrap mode tracks any rerun, even typed by hand).

**`lte-run [--] COMMAND`** works in any pane of a libtmux_easy session: it
reports the command's pid and exact status over the session's socket.
Elsewhere it just runs the command, same output and status. Ctrl-C, Ctrl-Z /
`fg` and stdin work as usual. Shell operators typed by hand (`lte-run a && b`)
supervise only `a`; `new_pane()` wraps the whole command in `bash -c`. Linux only.

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
| `j` / `k`, `Enter` | browse (the viewport follows); focus the pane |
| `g` | go to the pane in its own window; `prefix g` comes back |
| `x` / `r` | stop / rerun the pane |
| `X` / `Q` | stop all / stop all and kill the session (press twice) |
| `s`, `[` / `]`, `?`, `q` | zoom, resize, help, close |

- Colors: green running or exit 0, red failed, yellow stopped (by `x`, `X`,
  `stop_pane()`), magenta unknown.
- Actions reach the script over its socket: they work while it runs.
- `prefix g` is **borrowed** while the sidebar runs (`lte-key`) and returned
  after; nothing goes in `~/.tmux.conf`. `dashboard(key=...)` picks another.
- Turns on the session's `mouse` (`mouse=False` to skip).
- Needs Textual: `pip install textual` in the venv, or `pipx install 'libtmux-easy[dash]'`.
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
| `dash` | under `lte-session`: in its session and dashboard | kept until you leave |

Default `auto`: dash under lte-session, else tmux, else shell. Override the
`lte_pane_setup` fixture to run a setup command first (e.g. source an
environment), and `lte_output_checks` to fail on deaths only the output
shows (e.g. a supervisor's children). `tmux_session` gives a test a
`TmuxSession` of its own.

## lte-session

```bash
lte-session -- pytest -s tests/
```

- The command runs in the dashboard's home pane, a real terminal: `print()`,
  `input()`, pdb and Ctrl-C work. Panes the tests start appear in the sidebar.
- It gets your shell's environment and cwd, not the tmux server's.
- On detach (`prefix d`), its scrollback is printed in your terminal, and
  `lte-session` exits with the command's status. Still running: the output
  keeps streaming here. No TTY (CI): streams through. Inside tmux: switches
  your client instead of nesting.

| flag | |
|---|---|
| `--name NAME` | session name (default `lte_<command>_<pid>`) |
| `--keep` | leave the session up |
| `--width N` | sidebar columns |
| `--no-dash` | home pane only (also the fallback without Textual) |

## Examples

`demos/`: one runnable scenario per feature (`./run_all.py` runs them), the
dashboard, and the pytest plugin in all three modes.

## Design docs

`docs/`: architecture, exit status, wrap protocol, dashboard, lte-session,
the pytest plugin, and [known issues](docs/KNOWN_ISSUES.md).

## Compatibility

Linux. Tested with libtmux 0.10.1 to 0.62, tmux 3.3a and 3.4.

## License

Apache-2.0
