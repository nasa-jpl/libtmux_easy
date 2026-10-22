# pytest demos

The plugin (`libtmux_easy.testing`) starts the processes a test needs,
watches them, and stops them. **A process that dies, or prints a line you
said is bad, fails the test.** Turn it on in `conftest.py`:

```python
pytest_plugins = ["libtmux_easy.testing"]
```

## Start here

| demo | run |
|---|---|
| `basic/test_tour.py` | `pytest demos/pytest/basic/test_tour.py -v` |

A test class brings a system up in `setup_class`,
its tests use it, `teardown_class` shuts it down. Add `LTE_DEMO_CRASH=1`
to see a process die mid-test.

## How it fits together

```
setup_class      procs = new_processes(cls)       an empty group
                 procs.start(name, cmd, ready=)    start; wait for a line
                 procs.wait_until(fn)              a gate: any code
each test        self.procs.output/wait_for/stop/start
                 check_health()                    in long loops: fail now
after each test  plugin checks every group         a death fails THIS test
teardown_class   procs.close()                     stop; fail unless exit 0 on Ctrl-C
```

- **Names** are `window:pane`. In tmux, `servers:web` is pane `web` in a
  window `servers`. In shell mode, output lines are prefixed `[servers:web]`.
- **Where processes run** (same test code):

  | flag | processes run as | you see |
  |---|---|---|
  | (none) | subprocesses | their output in the terminal |
  | `--tmux` | panes under `lte-session`, attached | the dashboard; test output in the home pane |
  | `--tmux --no-attach` | panes in a detached tmux session | `tmux attach -t ...` (printed) |
  | `--keep-open` | as `--tmux`, never stopped | the session stays until you leave it |
  | under `lte-session` | panes in its session | the dashboard |

- **Logs**: each process's output goes to `<window>.<pane>.log` in pytest's
  tmp dir; the run ends with `process logs: <dir>`. Each process also gets
  `LTE_LOG_DIR`, e.g. to point a tool's own logs there.
- **Failing on output**: `FAIL_ON = {"*": r"Traceback", "sim:launch":
  some_function}`. A function gets each line and returns a problem or
  `None`, e.g. to parse a supervisor's "process died" lines.
- **Health**: each death or `FAIL_ON` hit fails one test: the one running
  when it happened. Between checks it is only recorded, so long tests call
  `check_health()` (or wait with `wait_until`, or a probe that checks).
- **On failure**: a failed test stops its class's (or module's) processes
  at once and skips the rest; define `abort()` on the class to bring them
  down your way first. `--keep-open` keeps them instead.
- **Also**: `exits=0` for a process that finishes (`wait_for_exit`),
  `stop_codes`, `wait_for("*", ...)`, `wait_for_in_order`, `arg()` for
  `--arg NAME=VALUE`, `@parametrize` for a class per value.

## Other ways to declare processes

| style | when | demo |
|---|---|---|
| `setup_class` + `new_processes(cls)` | a system per class (the tours) | `basic/test_tour.py` |
| `PROCESSES = {...}` + `processes` fixture | a system per module, no setup code | `basic/test_web.py` |
| `launcher` fixture | processes one test starts and stops | `basic/test_launcher.py` |
| `tmux_session` fixture | raw tmux panes (tmux only) | `lte_session/`, `dashboard/` |

All but `tmux_session` share the flags, logs and health checks above.

## All demos

| dir | what |
|---|---|
| `basic/` | no ROS: the tour, a module-wide web server, per-test processes |
| `dashboard/` | under `lte-session`: pdb, `input()`, the dashboard (see its docstring) |
| `lte_session/` | tests building their own tmux panes, under `lte-session` |

ROS 2 (a `launch_test` replacement on this plugin): the separate
`lte_launch_test` project. Reference: [docs/pytest-plugin.md](../../docs/pytest-plugin.md).
