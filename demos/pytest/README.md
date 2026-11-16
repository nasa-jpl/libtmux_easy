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
| `basic/test_web.py` | `pytest demos/pytest/basic/test_web.py -v`: the smallest class |
| `basic/test_tour.py` | `pytest demos/pytest/basic/test_tour.py -v`: everything else |

A test class brings a system up in `setup_class`, its tests use it, and the
plugin shuts it down after the last test (`teardown_class`, if any, first).
Add `LTE_DEMO_CRASH=1` to the tour to see a process die mid-test. The
plain-language overview: [TESTING.md](../../TESTING.md).

## How it fits together

```
setup_class      cls.procs = Processes(cls, fail_on_output=)  an empty group
                 web = procs.start(name, cmd, ready=)  start; wait for a line; a handle
                 procs.wait_for_condition(fn)                  a gate: any code
each test        plugin checks every group first        a death: the test errors
                 a new instance of the class: state lives on cls
                 web.wait_for_line/output/stop, procs.start
                 check_health()                        in long loops: fail now
after each test  plugin checks every group             a death fails THIS test
teardown_class   optional: procs.stop("w:*") ...       your own order
then             plugin stops the rest                 fail unless exit 0 on Ctrl-C
```

- **Names** are `window:pane`. In tmux, `servers:web` is pane `web` in a
  window `servers`.
- **Where processes run** (same test code):

  | flag | processes run as | you see |
  |---|---|---|
  | (none) | subprocesses | test output; process output in the logs |
  | `--echo PATTERN` | subprocesses | also output of matching processes, prefixed `[servers:web]`; `'*'`: all |
  | `--tmux` | panes under `lte-session`, attached | the dashboard; test output in the home pane |
  | `--tmux --no-attach` | panes in a detached tmux session, kept on failure | `tmux attach -t ...` (printed) |
  | `--keep-open` | as `--tmux`, not stopped after the tests | the session stays until you leave it |

- **Logs**: each process's output goes to `<window>.<pane>.log` in pytest's
  tmp dir; the run ends with `process logs: <dir>`. Each process also gets
  `LTE_LOG_DIR`, e.g. to point a tool's own logs there.
- **Failing on output**: `Processes(cls, fail_on_output=[r"Traceback",
  Only(some_function, name="sim:launch")])`, or `start(..., fail_on_output=)`. A function
  gets each line and returns a problem or `None`, e.g. to parse a
  supervisor's "process died" lines.
- **Health**: each death or `fail_on_output` hit fails one test: the one running
  when it happened. Between checks it is only recorded, so long tests call
  `check_health()` (or wait with `wait_for_condition`, or a probe that checks).
- **On failure**: a failed test skips the rest of its class. The processes
  stay up until `teardown_class`, where `procs.failed` says why; no stop is
  checked after a failure. `--keep-open` keeps them up after the run too.
- **Processes for one test**: `with Processes() as procs:` in the test.
  Stopped and checked at the end of the `with`; the class's keep running.
  Names are unique within a class.
- **Also**: `exits=0` for a process that finishes (`wait_for_exit`),
  `allowed_exit_codes`, `wait_for_line("*", ...)`, `mark()` and `since=`, `arg()` for
  `--arg NAME=VALUE`, `@parametrize` for a class per value.

## All demos

| demo | what |
|---|---|
| `basic/test_web.py` | the smallest class: one server |
| `basic/test_tour.py` | staged start, gates, `fail_on_output`, long tests, logs |
| `basic/test_per_test.py` | `Processes()` in a test: processes per test, restarts |
| `script_layout/` | a session script's system as data; the test `start()`s it |
| `script_build/` | the test runs the script's `build(session)`; also tests its runtime watches |
| `script_adopt/` | the script run as it is; the test adopts the panes it made |
| `dashboard/` | with `--tmux -s`: pdb, `input()`, the dashboard (see its docstring) |

ROS 2 (a `launch_test` replacement on this plugin): the separate
`lte_launch_test` project. Reference: [docs/pytest-plugin.md](../../docs/pytest-plugin.md); the
`script_*` demos: [docs/testing-scripts.md](../../docs/testing-scripts.md).
