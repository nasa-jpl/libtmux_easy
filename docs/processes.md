# Processes outside pytest

`libtmux_easy.processes.Processes` is the engine under the pytest plugin, usable by any
program (a launcher, a script). `libtmux_easy.testing.Processes` is a subclass that adds a
test owner, the command-line mode and pytest failures.

```python
from libtmux_easy.processes import Processes, ProcessFailure

procs = Processes("bringup", fail_on={"*": r"Traceback"},
                  on_exit=lambda name, status, died: ...)
procs.start("sim:server", "./server", ready=r"listening", env={"RATE": "10"})
procs.wait_until(server_answers)     # raises ProcessFailure on any death
procs.close()                        # SIGINT all at once, then SIGTERM, SIGKILL
```

| Argument | Meaning |
|---|---|
| `name` | The group's name: log dir prefix, tmux window prefix |
| `tmux=` | A `TmuxSession`: wrap-mode panes. None: subprocesses |
| `prefix=` | tmux window prefix (default `name`); `""`: bare window names |
| `echo=`, `tty=` | Subprocesses: print output of matching names to `tty` (stdout) |
| `on_exit=` | `on_exit(name, PaneStatus, died)` from a watcher thread, on every exit |
| `fail_on=`, `stop_codes=`, `sigint_timeout=`, `setup=`, `log_dir=` | As in the plugin |
| `keep_open=` | `close()` stops nothing |

Same methods as the plugin's (`start`, `build`, `adopt`, `wait_for`, `wait_until`, `stop`,
`check`, ...). Differences:

| | `processes.Processes` | `testing.Processes` |
|---|---|---|
| A failure | `ProcessFailure` (an `AssertionError`) | `pytest.fail` / `AssertionError` |
| Mode | `tmux=` argument | `--tmux` / subprocess from the command line |
| `build()` | Needs `tmux=` | Uses the run's session |
| Owner, abort on a failed test | No | Yes |

Extras: `start(env=, sigint_timeout=)` (a process's own environment and SIGINT grace;
`close()` still stops all at once, each escalating on its own deadline), `kill_all()` (SIGKILL everything now, e.g. a second Ctrl-C while
`close()` waits; not a death). `close()` and `stop_all` stop every process in parallel.
