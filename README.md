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
| `new_pane(name, cmd, enter=True)` | Create pane in current window |
| `send_keys(cmd, target_pane=None)` | Send command |
| `send_command_before(cmd, window=None)` | Prefix for new panes |
| `select_window(name)` | Switch window |
| `select_pane(name)` | Switch pane |
| `attach()` | Attach to session |

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
