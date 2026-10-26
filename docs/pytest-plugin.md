# pytest plugin (`libtmux_easy.testing`)

A `launch_testing` replacement: a test brings up the processes it needs,
talks to them, and **a process that dies fails the test**. Every process is
a shell command. Demos: [demos/pytest](../demos/pytest). ROS 2 lives in a
separate project, `lte_launch_test`, built on this.

```python
# conftest.py
pytest_plugins = ["libtmux_easy.testing"]

# test_system.py
from libtmux_easy.testing import Processes, check_health

class TestSystem:
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls, fail_on={"*": r"Traceback"})   # empty; logs in a tmp dir
        cls.procs.start("sim:server", "./server", ready=r"listening")
        cls.procs.wait_until(server_answers, timeout=30)   # a gate: any code
        cls.procs.start("test:client", "./client")
        cls.procs.start("test:migrate", "./migrate", exits=0)  # finishes on its own

    @classmethod
    def teardown_class(cls):
        cls.procs.close()                     # stop; fail unless each stops cleanly

    def test_long(self):
        while running():
            check_health()                    # a death fails it now

    def test_helper(self):
        with Processes() as procs:            # processes for this test only
            probe = procs.start("tools:probe", "./probe", ready=r"up")
            probe.wait_for(r"done")           # start() returns a Process handle
```

## Options

| option | processes run as | you see |
|---|---|---|
| (none) | subprocesses | test output; process output only in the logs |
| `--tmux` | panes under `lte-session` (pytest reruns itself there), attached | the dashboard; test output in the home pane |
| `--tmux --no-attach` | panes in a detached session `lte_test_<pid>`, kept on failure | test output; `tmux attach -t ...` is printed |
| `--keep-open` | as `--tmux`, never stopped | inspect after the run |
| `--arg NAME=VALUE` | | read with `arg(NAME, default)`, typed like the default; repeatable |
| `--echo PATTERN` | subprocesses | also the output of processes whose `window:name` matches (fnmatch), prefixed `[window:name]`; `'*'`: all; repeatable |

`--no-attach` and `--keep-open` imply `--tmux`. Without a terminal (CI),
`--tmux` is `--tmux --no-attach`. Under `lte-session`, processes always
join its session and dashboard.

## Declaring processes

| where | owner | stopped |
|---|---|---|
| `Processes(cls, ...)` in `setup_class` | the class | `close()` in `teardown_class`; if setup fails, when the class ends |
| `with Processes(...) as procs:` in a test | the test | at the end of the `with` (checked); without `with`, after the test |

One class, the same arguments, nothing to import but `Processes`; no
fixtures. Outside a test and without an owner (a fixture, `setup_class`),
`Processes()` raises `TypeError`.

**Names** are `window:pane`; a name without `:` gets a window of its own.
They are unique within a class: a test's group can't start a name the
class's group has (`ValueError: ... is taken`). tmux windows are
`<Class>/<window>`, shared by the class's groups, or `<test>/<window>` for
a test outside a class; `#N` is added if a class name is taken.

`@parametrize("rate", [5, 10])` on a class runs it all, `setup_class` to
`teardown_class`, once per value: `TestX[rate=5]`, `TestX[rate=10]`, the
value as `cls.rate`. Several names (`"a, b"`, tuples) or stacked
decorators multiply.

## How a test class runs

pytest runs a class like this (*verified*, pytest 9):

```
setup_class(cls)        once, before the first test
for each test:
    t = TestX()         a NEW instance for every test
    t.test_x()
                        the plugin checks every group (a death fails test_x)
teardown_class(cls)     once, after the last test
```

| rule | so |
|---|---|
| a new instance per test | state for the class goes on `cls` in `setup_class` (`cls.procs = ...`); `self.procs` in a test reads that class attribute |
| `self.x = ...` in a test lives on that test's instance | gone by the next test; don't use it to pass things between tests |
| shared objects stay shared | `self.procs.stop()` / `start()` in one test changes what later tests see. Tests run in file order |
| no `__init__` | pytest doesn't collect a class that has one |
| `setup_class` takes no fixtures | the plugin finds its config and temp dirs itself: `Processes(cls)` needs only the class |
| `setup_class` raises | every test in the class ERRORs; pytest skips `teardown_class`; the plugin stops what was started when the class ends |
| a test fails | the class's processes are stopped at once and its remaining tests SKIPPED (see Health and failure) |

pytest runs the two methods as a class-scoped fixture internally. We use
them, not a fixture, because they read as plain Python: two named
methods, no decorators, no `yield`.

## `Processes`

`Processes(owner=None, *, fail_on=None, stop_codes=None, sigint_timeout=10, on_abort=None, log_dir=None)`

| argument | meaning |
|---|---|
| `owner` | the test class (`cls` in `setup_class`); none in a test: that test. Its failed tests abort the group |
| `fail_on` | `{name or "*": regex, function line -> problem or None, or a list}`: a match is a death |
| `stop_codes` | `{name or "*": codes}` allowed when stopped (default 0); a signal N is 128+N, so 130 allows dying of SIGINT |
| `sigint_timeout` | seconds SIGINT gets before SIGTERM |
| `on_abort` | called with no arguments when a test fails, before everything is stopped: bring things down your own way |
| `log_dir` | where the logs go; default `<basetemp>/<Class>N/`, `<Class>.<test>N/` or `<test>N/` |

| method | does |
|---|---|
| `start(name, cmd, ready=, timeout=30, fail_on=, exits=, stop_codes=)` | start; returns a `Process`. `ready`: wait for a matching line. `fail_on`: more patterns for it. `exits`: codes it may finish with on its own (no death). `stop_codes`: as above, for it |
| `stop(name, sigint_timeout=, check=True)` | SIGINT, then SIGTERM, SIGKILL; `check`: fail unless it stopped cleanly |
| `close(check=True)` | stop everything still running; `check`: fail unless each stopped cleanly. Again: nothing |
| `wait_for(name, regex, timeout)` | a line matching (`re.Match`); name `"*"`: any process |
| `wait_for_in_order(name, [regex, ...], timeout)` | lines in that order, each later than the last |
| `wait_for_exit(name, timeout)` | an `exits=` process finishing; fails on a wrong code |
| `wait_until(fn, timeout)` | `fn()` until truthy, checking health between calls |
| `output(name)`, `log(name)`, `status(name)` | what it printed (latest start), its log file, its `PaneStatus` |
| `check()` | raise if anything died or matched `fail_on` since the last check |
| `build(fn, ..., exits=)`, `adopt(cmd, ...)` | a session script's processes: see below |
| `procs["window:pane"]` | its `Process` |
| `kill(name)` | SIGKILL: a death, unless in `expect_exit` |
| `with expect_exit(name, timeout=10):` | an exit you cause on purpose (a kill, a bad input) is no death; fails if it hasn't exited by the end |

`Process`, from `start()` or `procs[name]`: the methods above for its name,
without it: `stop()`, `kill()`, `wait_for(regex)`, `wait_for_in_order()`,
`wait_for_exit()`, `output()`, `log()`, `status()`, and `.name`; `start()`
starts it again with the same command and options. It follows the name, so
it stays valid after a stop and a new start.

## Testing a session script

A session script (a libtmux_easy script that builds a tmux session) is
like a `ros2 launch` file: it brings a system up. Three ways to test one,
from most control to least:

| method | the script | the test | demo |
|---|---|---|---|
| 1. layout | `panes()` returns `[("window:pane", cmd), ...]`; `main()` lays it out | `start()` each: `ready=`, gates, restarts; any mode | `demos/pytest/script_layout` |
| 2. build | `build(session)` makes the windows and panes; `main()` adds runtime extras (dashboard, watches) | `procs.build(build)`: the script's own code on a session the plugin controls | `demos/pytest/script_build` |
| 3. adopt | anything that makes a tmux session, unchanged | `procs.adopt("python3 launch.py")`: run it, watch what it made | `demos/pytest/script_adopt` |

**2. `build(fn, *args, exits=None, **kwargs)`** calls `fn(session, *args,
**kwargs)`. Each `new_pane()` becomes a process of the group, named by the
script (`sim:gazebo`), logged from its first byte; windows are
`<Class>/<window>` in the run's tmux session (always tmux). Panes default
to wrap mode. `attach()`, `dashboard()` and the kill/close calls do
nothing; `send_command_before()`, `copy_environment()` and
`set_default_mode()` apply to the script's windows only. `procs.session`
is that session, for the script's other functions:

```python
cls.procs.build(launch.build, port=PORT, exits={"jobs:migrate": 0})
launch.monitor(cls.procs.session, alert=cls.alerts.append)   # the script's own watches
with cls.procs.expect_exit("workers:ticker"):
    cls.procs["workers:ticker"].kill()                       # does monitor() notice?
```

**3. `adopt(cmd, name="script", ready=None, timeout=60, exits=None)`** runs
`cmd` as process `name` on a tmux server of its own (`TMUX_TMPDIR`): its
session names can't clash with yours, and none of it shows in the run's
dashboard. `LTE_HEADLESS=1` makes `attach()` and `dashboard()` do nothing.
The script must exit 0 within `timeout`; or, with `ready=`, print a
matching line, and it stays a process of the group. Then every pane on
that server is a process, named `window:pane` (`new_pane()`'s name, else
the pane index). Returns their `Process`es.

| what adopt knows | wrap pane | exec pane | shell pane, plain tmux |
|---|---|---|---|
| running, deaths | yes | yes | yes (the shell has children) |
| exit code | exact (lte-run writes it to `$LTE_RUN_STATUS_DIR`) | exact (tmux; see KNOWN_ISSUES) | unknown; a SIGINT stop counts as clean |
| output | from the first byte if the script captures (`capture=True`), else the pane's history, then a pipe | same | same |
| stop | Ctrl-C into the pane, then SIGTERM, SIGKILL | same | same |
| restart | no | no | no |

On failure the server is kept: `tmux -S <socket> attach`, printed at the end.

Module functions: `check_health()` (every open group), `arg(name,
default)`, `parametrize(names, values)`.

## Health and failure

| when | what | outcome |
|---|---|---|
| a process exits, not stopped and not an expected `exits=` code | recorded (exit watch) | a death |
| a `fail_on` match | recorded (output watch) | a death |
| in the test: `check()`, `check_health()`, `wait_until`, `wait_for*` | checked | FAILED now |
| after each test | `pytest_runtest_call` checks every open group | FAILED, with the last output and the log's path |
| a test (or `setup_class`) fails | `pytest_runtest_makereport`: `on_abort()`, then `close(check=False)` on the class's and the test's groups | stopped at once, no checks; the rest of the class SKIPPED |
| `teardown_class`: `close()` | SIGINT, escalating, in parallel | ERROR unless each stopped with a `stop_codes` code; nothing to do after an abort |

Each problem is reported once, by the first check after it happened.
`--keep-open` turns failure handling off: nothing is stopped.

## Logs

Each process's output, as plain text, goes to `<window>.<name>.log` in
the group's `log_dir`.
The summary prints `process logs: <dir>`; a death's report names the file.
Each process gets `LTE_LOG_DIR`, its log's directory (e.g. for a tool's
own logs).

## Hook for the app

| fixture | scope | default | use |
|---|---|---|---|
| `lte_pane_setup` | session | `None` | shell command before each process (`cd`, `export`) |

Processes get exactly pytest's environment in every mode (tmux panes: see
`environment.md`), minus `BASH_ENV`: a container's that sources ROS would
drop the overlay.

## Design

- **Modes.** `subprocess` (default), `tmux` (a detached session), `dash`
  (under lte-session). Chosen in `pytest_configure` from the flags; not an
  option of its own.
- **Backends.** `_SubprocessBackend`: `Popen(["bash", "-c", cmd])` per
  process, a reader thread (plain text, log, `fail_on`, `--echo`) and a
  waiter thread (exit). `_TmuxBackend`: wrap-mode panes (`lte-run --tee
  <log>`), exit and output watches from `TmuxSession`. `Processes` holds
  what is common: names, `_Run` per start (stopping, died, finished),
  deaths, `fail_on` hits, report-once counters.
- **Reading output.** Subprocess: the reader's lines. tmux: the `--tee`
  log, not `capture-pane`, which also holds the shell's prompt and the typed
  command. `--tee` rewrites the log on each start, so it holds the latest
  start.
- **One registry.** Every open `Processes` is in `_open`; `check_health()`
  and the hooks walk it, so helpers deep in test code need no handle.
  `Processes()` works outside fixtures through `_plugin`, filled by a
  session autouse fixture before any `setup_class`.
- **Ownership.** A group's `owner` (the class, or the test item) decides
  which failures abort it and which tests are skipped after. `Processes()`
  finds its test through `pytest_runtest_call`, which records the running
  item and, after a passing test, closes (and checks) the test's groups
  still open.
- **`--tmux` attach.** `pytest_cmdline_main` (tryfirst) execs
  `python -m libtmux_easy.launch -- python -m pytest <same args>`. pytest
  already captures fds 0-2 there: the tty check uses `/dev/tty` and the
  foreground process group, and capture (stdin included) is suspended
  before the exec so the new process gets the real terminal.
- **`parametrize`.** A `pytest_pycollect_makeitem` hook turns a marked class
  into one `pytest.Class` node per value, each over a subclass with the
  values as attributes (`_ParametrizedClass._getobj`: pytest 9 looks a
  class up in its module by name otherwise). Each value's subclass is its
  own owner, so a failure at one value doesn't skip the others.
- **Concurrency.** Sessions are `lte_test_<pid>` (`-N` if taken, never
  killed); window prefixes get `#N` if taken. Ports and fixed paths are the
  test's job (derive them from `os.getpid()`).

## Subprocesses: signals (*verified*, ROS 2 Jazzy)

- `ros2 launch` with a non-tty stdin is "noninteractive" and re-sends SIGINT
  to children that already got the group's. Python nodes die (-2) of the
  second. `ros2 run` doesn't forward SIGINT at all.
- So each process runs with `start_new_session` and a **pty for stdin**, and
  is stopped with `killpg`: SIGINT, then SIGTERM (5 s), then SIGKILL (2 s).
- `ros2 launch` exits 0 on SIGINT and keeps running after a child dies; the
  only record is `[ERROR] [name-N]: process has died [pid P, exit code C`.
  Hence `fail_on` functions (`lte_launch_test.launch_death`).
- A bare `rclpy.spin()` dies *by* SIGINT; catch `KeyboardInterrupt` /
  `ExternalShutdownException` to exit 0, or allow 130 in `stop_codes`.
