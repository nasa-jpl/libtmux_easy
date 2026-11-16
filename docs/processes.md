# ProcessGroup: processes outside pytest

`libtmux_easy.processes.ProcessGroup` is the engine under the pytest plugin, usable by any
program (a launcher, a script). `libtmux_easy.testing.Processes` is a subclass that adds a
test owner, the command-line mode and pytest failures.

```python
from libtmux_easy.processes import ProcessFailure, ProcessGroup

procs = ProcessGroup("bringup", fail_on_output=r"Traceback")
procs.on_exit(lambda name, status, died: ...)   # every exit, as it happens
procs.start("sim:server", "./server", ready=r"listening", env={"RATE": "10"})
procs.wait_for_condition(server_answers)     # raises ProcessFailure on any death
procs.stop("sim:*")                  # a glob: every match at once; the group stays usable
procs.close()                        # the end: stop the rest, let go of the group
```

| Argument | Meaning |
|---|---|
| `name` | The group's name: log dir prefix, tmux window prefix |
| `tmux=` | A `TmuxSession`: wrap-mode panes. None: subprocesses |
| `prefix=` | tmux window prefix (default `name`); `""`: bare window names |
| `echo=`, `tty=` | Subprocesses: print output of matching names to `tty` (stdout) |
| `fail_on_output=`, `allowed_exit_codes=`, `sigint_timeout=`, `sigterm_timeout=`, `setup=`, `log_dir=` | As in the plugin |
| `keep_open=` | `close()` stops nothing (in tests: nor `teardown_class`'s stops) |
| `env=` | Variables for every process of the group (None unsets); see below |

`on_exit(fn)` calls `fn(name, PaneStatus, died)` on every exit, from a
watcher thread (a decorator too); `remove_on_exit(fn)` undoes it. Plus the
plugin's methods (`start`, `build`, `adopt`, `wait_for_line`, `mark`, `wait_for_condition`, `stop`,
`check`, ...). Differences:

| | `processes.ProcessGroup` | `testing.Processes` |
|---|---|---|
| A failure | `ProcessFailure` (an `AssertionError`) | `pytest.fail` / `AssertionError` |
| Mode | `tmux=` argument | `--tmux` / subprocess from the command line |
| `build()` | Needs `tmux=` | Uses the run's session |
| Owner, `failed`, skip after a failed test | No | Yes |
| Ended by | `close()`, or `with` | the plugin |

Extras: `start(env=, sigint_timeout=, sigterm_timeout=, restart=, restart_delay=)` (a process's own environment,
SIGINT and SIGTERM grace, reruns on failure (`lte-run --restart`, `wrap-mode.md`), kept by `Process.start()`).

## Stopping and ending

| call | does |
|---|---|
| `stop(name)` | SIGINT, then SIGTERM, SIGKILL; checked. A glob (`"sim:*"`): every running match at once, `{name: PaneStatus}` |
| `stop_all()` | `stop()` on everything still running, in parallel, each on its own deadlines |
| `Process.start(cmd=None)` | start it again after a stop; a new command if given |
| `close()` | the end: `stop_all()`, then the group lets go: names freed, out of `check_health()`, `adopt()`'s tmux server killed; `start()` raises after it. Again: nothing |
| `kill_all()` | SIGKILL everything now, e.g. a second Ctrl-C while `stop_all()` waits; not a death, not checked |

`check=False` skips the check for one call; `allowed_exit_codes=ANY_EXIT`
(or `"any"`) makes any end clean for the processes it covers. With
`failed` set, no stop is checked.

## Environment

A process's environment, later wins (None unsets, at any level):

| level | set by |
|---|---|
| inherited | `os.environ` of this process |
| group | `ProcessGroup(env=)` |
| process | `start(env=)` |

- Never written to `os.environ`: other groups, classes and tests don't see it.
- Same in every mode; tmux panes get it exactly (`environment.md`).
- `procs.env` is a read-only view, fixed when the group is made.
- `Process.start()` gets it again.
- `build()` panes: group < `copy_environment()` < `new_pane(env=)`.
- `adopt()`: the script gets it; its panes get what the script passes on.

## tmuxp configs

No native loader. Two ways:

- `procs.adopt("tmuxp load -d config.yaml", exits={"w:0": 7})`: works as for any session
  script (it runs on the group's own tmux server). Panes are named `window:index`. Only
  commands written as `lte-run CMD` in the YAML get exact exit codes; the rest are shell panes.
- Rewrite it with `start()` or `build()`: usually as short, and every process is exact.
