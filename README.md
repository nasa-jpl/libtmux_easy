# libtmux-easy

Build tmux sessions from Python and supervise what runs in them:

- **exact exit status** for every pane, and an event the moment one exits;
- panes stay **interactive**: attach, type, Ctrl-C, pdb;
- a live **dashboard** of every pane;
- **output** captured to files, searched, and watched for patterns;
- a **pytest plugin** that launches the processes a test needs and fails
  the test if one dies.

Linux only.

## Install

| you want | install |
|---|---|
| library and commands, in a venv | `pip install 'libtmux-easy[dash]'` |
| the commands only, any distro | `pipx install 'libtmux-easy[dash]'` |
| Ubuntu system Python (ROS) | both `.deb`s from [Releases](https://github.com/nasa-jpl/libtmux_easy/releases) |
| to develop | `uv sync` in a checkout |

```bash
pip install 'libtmux-easy[dash]'
# not on PyPI yet:
pip install 'libtmux-easy[dash] @ git+https://github.com/nasa-jpl/libtmux_easy'
# Ubuntu:
sudo apt install ./python3-libtmux-easy_0.2.0_all.deb ./libtmux-easy-dash_0.2.0_all.deb
```

- Needs tmux. `[dash]` adds Textual for the dashboard; everything else works without it.
- Wrap-mode panes run `lte-run`: it must be on the panes' PATH, not only the
  script's (the `.deb`, pipx, or `send_command_before("source .venv/bin/activate")`).
- ROS: a venv made with `python3 -m venv --system-site-packages` sees `rclpy`.
- More (pipx with the `.deb`, `--user` installs, building the `.deb`s):
  [docs/packaging.md](docs/packaging.md).

## Quick start

```python
from libtmux_easy import TmuxSession

session = TmuxSession.create("my-session")
session.new_window("work")
session.new_pane("editor", "nvim")
server = session.new_pane("server", "python3 -m http.server", mode="wrap")

session.new_window("logs", layout="main-horizontal")
session.new_pane("journal", "journalctl -f")

session.wait_for_output(server, r"Serving HTTP", timeout=10)
session.attach()
```

Panes are named `window:pane`; anything that takes a pane also takes that
name, its id (`%3`) or the `Pane`. Each pane's border shows its state
(`server [running]`, `server [exited 3]`).

## Pane modes

| `mode=` | the command | exit status | interactive |
|---|---|---|---|
| `"shell"` (default) | is typed into bash | from `$?`: a signal death is *inferred* from 128+N | yes |
| `"exec"` | is the pane's process | from tmux; exact, but tmux sometimes loses it | no |
| `"wrap"` | is typed as `lte-run <cmd>` | from `lte-run`, exact | yes |

Use **wrap** when the status matters. `create(mode=)` or
`set_default_mode()` sets the default. Rerun a pane with `respawn_pane()`:
the library tracks the new run (wrap mode also tracks reruns typed by hand).

## API

| method | |
|---|---|
| `TmuxSession.create(name, env=None, mode="shell", capture=None, status_file=None)` | a new session (replaces one of that name) |
| `TmuxSession.current(env=None, mode="shell")` | adopt the session this process runs in |
| `new_window(name, layout="tiled", env=None, capture=None)` | a window; the next panes go in it |
| `new_pane(name, cmd, mode=None, on_exit=None, on_output=None, env=None, capture=None, tee=None)` | a pane running `cmd` |
| `send_keys(cmd, target_pane=None)`, `send_command_before(cmd, window=None)` | type into a pane; set up every new pane |
| `copy_environment(env=None)` | `env` (default `os.environ`) for every later pane |
| `is_running(pane)`, `exit_status(pane)`, `pane_status(pane)`, `status()`, `print_status()` | state: `ExitStatus` (`.code`, `.signal`, `.ok`), `PaneStatus` (`.state`: running / stopped / ok / failed / unknown) |
| `watch_pane(pane, on_exit)`, `wait_for_exit(pane, timeout)` | exit events |
| `stop_pane(pane)`, `stop_all()` | SIGINT, then SIGTERM, then SIGKILL; `stop_all()` in parallel |
| `respawn_pane(pane, cmd=None)` | rerun in place, any mode |
| `capture(target, path)`, `search_output(target, pattern)`, `output_text(pane)` | output to files; grep them |
| `watch_output(target, pattern, cb)`, `wait_for_output(target, pattern, timeout)`, `expect(target, pattern)` | output events |
| `dashboard()`, `dashboard_pane(cmd)`, `close_dashboard()` | the dashboard |
| `on_close(action)`, `close()`, `bind_close_key(key=None)` | graceful close |
| `kill_session()`, `release(keep_windows=False)` | end, or let go and leave windows up |
| `select_window()`, `select_pane()`, `attach()` | |

`env=` (e.g. `os.environ`) works at session, window and pane level, later
levels winning: panes otherwise get the tmux *server's* environment, which
may predate an activated venv. `status_file=` writes `status()` as JSON on
every change, for other programs.

`kill_session()` only sends SIGHUP: `stop_all()` or `close()` first for a
clean shutdown.

## Output

```python
sess.new_pane("api", cmd, on_output={r"Traceback": on_crash})  # callback per matching line
m = sess.wait_for_output("main:web", r"port (\d+)", timeout=10)  # what it printed already counts
with sess.expect(pane, r"pong"):                               # watch first, then act
    sess.send_keys("ping", target_pane=pane)

cap = sess.capture("main", "/var/log/app")   # a window's panes to files, from now on
sess.search_output("main", r"ERROR")         # [OutputMatch], later
```

`target` is a pane, a window, or `None` for the whole session (panes made
later too). tmux pipes each captured pane to a file (`pipe-pane`); matching
is on plain text, colors dropped. Unwatched panes cost nothing. Details:
[docs/output-match.md](docs/output-match.md).

## Dashboard

```python
session.dashboard()                                       # window "dash", always first
session.dashboard_pane("htop", title="load", size="30%")  # optional, beside the viewport
```

Left: a sidebar of every pane by window, with running / stopped / failed
counts. Right: the selected pane itself, swapped in (`tmux swap-pane`), so
it is fully interactive. `home` shows the script's stdout and logging.

| key | action |
|---|---|
| `Up` / `Down`, `Enter` | browse (the viewport follows); focus the pane |
| `g` | go to the pane in its own window; `prefix g` comes back |
| `x` / `r` / `c` | stop / rerun the pane / copy its command |
| `X` `X` / `Q` `Q` | stop all / close the session gracefully |
| `[` / `]`, `?`, `q` | resize, help, quit |

Needs Textual. Options: `width=`, `initial=pane`, `mouse=`. More:
[docs/dashboard.md](docs/dashboard.md).

## Testing with pytest

```python
# conftest.py
pytest_plugins = ["libtmux_easy.testing"]

# test_web.py
PROCESSES = {"servers:web": "python3 -m http.server 8000"}
READY = {"servers:web": r"Serving HTTP"}  # optional: wait for it first
FAIL_ON = {"*": r"Traceback"}             # optional: a match fails the test

def test_serves(processes):
    ...          # processes.output("servers:web"), .wait_for(name, pattern), .check()
```

`processes` (module scope) starts them and waits for `READY`. A test fails
if a process exits early or prints a `FAIL_ON` line, even if the test never
noticed. Teardown stops them, and errors unless each exited 0 on SIGINT.

| `--lte-mode` | processes run as | after the run |
|---|---|---|
| `shell` | subprocesses, output prefixed `[name]` | stopped |
| `tmux` | wrap panes in a detached session | kept on failure (`--lte-keep`) |
| `dash` | panes in `lte-session`'s session and dashboard | kept with `lte-session --keep` |

Default: dash under lte-session, else tmux, else shell. Fixtures to
override, and `tmux_session` for a test's own session:
[docs/pytest-plugin.md](docs/pytest-plugin.md).

## Closing a session

tmux has no hook before a session closes, so a graceful close is a command:
`lte-close` runs the close actions, stops every pane (Ctrl-C, then SIGTERM,
SIGKILL) and kills the session, showing its progress in a `close` window.

```python
sess.on_close("./cleanup.sh")                         # shell: runs even if the script died
sess.on_close(lambda s: save_logs(s), when="before")  # Python: while the script runs
sess.close()                                          # or close from the script
```

`prefix Q` (sessions from `create()` or `lte-session`) and the dashboard's
`Q` `Q` run `lte-close`. Every way to stop and close:
[docs/session-close.md](docs/session-close.md).

## Commands

| command | does |
|---|---|
| `lte-run [--tee FILE [-a] [--raw]] [--] CMD` | runs CMD, reporting its pid and exact status to the session (wrap mode); elsewhere just runs it. `--tee` also copies the output to FILE as plain text (`-a` appends) |
| `lte-session [--keep] [--name N] [--no-dash] -- CMD` | runs CMD (e.g. `pytest -s`) in a new dashboard session's home pane: a real terminal, so `input()` and pdb work; panes its tests start appear in the sidebar. Exits with CMD's status |
| `lte-close [-t SESSION]`, `lte-close add -- CMD` | close a session gracefully; add a close action |
| `lte-dash` | the dashboard sidebar (started by `dashboard()`) |
| `lte-key borrow KEY OPTION -- CMD` | bind `prefix KEY` in sessions where OPTION is set, the old binding elsewhere |
| `lte-defaults` | print the starter tmux settings |

`lte-run` keeps the command's terminal: Ctrl-C, Ctrl-Z and stdin work as
usual. See [docs/wrap-mode.md](docs/wrap-mode.md) and
[docs/lte-session.md](docs/lte-session.md).

## tmux defaults

A tmux server with **no config file** gets starter settings: vi copy mode,
`prefix |` / `-` split, `prefix h j k l` move, `prefix C-x` kill-session,
`mouse on`. `LTE_TMUX_DEFAULTS=0` turns them off; a server with a config is
left alone. The library's own keys are borrowed only if unbound, and
`~/.tmux.conf` can move them:

| key | in lte sessions | to change |
|---|---|---|
| `prefix g` | back to the dashboard | `set -g @lte_key_dash G` |
| `prefix Q` | close gracefully | `set -g @lte_key_close ""` (none) |

[docs/tmux-defaults.md](docs/tmux-defaults.md).

## More

- `demos/interactive/`: guided tours, one per feature; start with `01_session.py`.
  `demos/examples/`: plain scripts.
- `docs/`: how it works and why ([index](docs/README.md)), and
  [known issues](docs/KNOWN_ISSUES.md).
- Tested with libtmux 0.17 to 0.62, tmux 3.3a and 3.4, Python 3.8 to 3.13.
  The `.deb`s need Ubuntu 24.04 or newer; on 22.04, use pipx or pip.

## License

Apache-2.0
