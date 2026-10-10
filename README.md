# libtmux-easy

Python wrapper for [libtmux](https://github.com/tmux-python/libtmux) - configure tmux sessions with code instead of YAML.

## Quick Start

```python
from libtmux_easy import TmuxSession

session = TmuxSession.create("my-session")

session.new_window("work")
session.new_pane("editor", "nvim")
session.new_pane("shell", "ls -la")

session.new_window("monitoring", layout="main-horizontal")
session.new_pane("logs", "journalctl -f")
session.new_pane("htop", "htop")

session.attach()
```

## Installation

```bash
pip install libtmux-easy
```

## Key Features

- Stateful API - operations target current window/pane
- Named panes - reference by `window:pane` instead of IDs
- Version compatible - tested with libtmux 0.10.1 to 0.53.1
- Layout support - tiled, main-horizontal, main-vertical, etc.

## API

| Method | Description |
|--------|-------------|
| `TmuxSession.create(name)` | Create session |
| `new_window(name, layout=None)` | Create window |
| `new_pane(name, cmd, enter=True, mode="shell")` | Create pane in current window |
| `send_keys(cmd, target_pane=None)` | Send command |
| `send_command_before(cmd, window=None)` | Prefix for new panes |
| `select_window(name)` | Switch window |
| `select_pane(name)` | Switch pane |
| `attach()` | Attach to session |
| `is_running(pane)` | True if the pane's command is still running |
| `exit_status(pane)` | `ExitStatus` for a finished pane (code/signal/ok) |
| `respawn_pane(pane, cmd=None)` | Rerun a pane's command in place (any mode) |

## Pane Modes

`new_pane(..., mode=...)` controls how the command runs:

- `mode="shell"` (default) -- command is typed into a bash prompt. Interactive:
  stop, edit, and rerun by hand. `exit_status()` reads the shell's `$?`, so a
  signal death (e.g. Ctrl-C) is *inferred* from the 128+N convention and can't
  be told apart from a real `exit 130`.
- `mode="exec"` -- command becomes the pane's own process, and tmux records
  its outcome, so `exit_status()` separates an exit code from a killing signal.
  No shell to type into. See the known issues below before relying on it.
- `mode="wrap"` -- like shell mode, but the command is typed as
  `lte-run <cmd>`. The wrapper is the command's real parent and reports its pid
  and exact status over a socket. Interactive, exact, no tmux bookkeeping.
  Costs one ~13 MB helper process per pane.

| situation | mode |
|---|---|
| poking around interactively | `shell` |
| the status must be exact and reliable, and the pane should stay usable | `wrap` |
| no interaction needed, occasional lost status acceptable | `exec` |

Rerun with `respawn_pane()` in any mode. In shell and exec mode, plain
`send_keys()` restarts the command but the library goes on reporting the
previous run's status. Wrap mode tracks any rerun, even one typed by hand.

### `lte-run`

```bash
lte-run [--] COMMAND [ARG...]
```

- Prepend it to anything typed in **any** pane of a libtmux_easy session. It
  finds the session via the `@lte_dir` session option.
- Outside a session, or once the session's script has exited, it just runs the
  command: same output, same exit status. Always safe to prepend.
- Exits like its command did (a signal death is re-raised), so
  `lte-run a && b` and Ctrl-C in a loop behave normally.
- Ctrl-C, Ctrl-Z / `fg` and stdin all work as without it.
- Installed as a console script. From a bare checkout, the library types
  `python3 .../src/libtmux_easy/wrap.py` instead.
- Shell operators typed by hand (`lte-run a && b`) supervise only `a`.
  `new_pane()` wraps the whole command in `bash -c` when it sees one.
- Linux only (`/proc`, `$TMUX_PANE`).

### Known issues: exec mode

- **tmux can lose the exit status.** Occasionally tmux never reaps the pane's
  process: it stays a zombie, `#{pane_dead}` becomes 1, but
  `#{pane_dead_status}` / `#{pane_dead_signal}` are never set. Only the parent
  can read a zombie's status, so it is gone for good, and `exit_status()`
  returns unknown and logs a warning. Reproduced with raw tmux 3.3a and no
  library code involved, after both signal deaths and normal exits. On tmux
  3.3a it was rare (0 in 65 one-at-a-time exits); on tmux 3.4 a single
  SIGINT'd exec pane lost its status 3 times in 4. Root cause unknown. Use
  wrap mode when the status matters.
- **`pane_dead` does not mean the status is ready.** tmux sets it when the
  pane's pty closes, and records the status only once it reaps the process.
  These are separate events, so don't treat one as implying the other.
- **Not interactive.** There is no shell, so you can't edit and rerun by hand.

## Examples

### Command Prefix
```python
session.send_command_before("cd /app && source venv/bin/activate")
session.new_pane("server", "python app.py")  # runs in /app with venv
```

### Target Specific Pane
```python
session.new_pane("p1", "echo one")
session.new_pane("p2", "echo two", target="window1:p1")  # split from p1
session.send_keys("ls", target_pane="window1:p2")
```

## Compatibility

Tested on Ubuntu/Debian with libtmux versions 0.10.1, 0.21.0, 0.53.1. Handles API differences automatically.

## License

MIT
