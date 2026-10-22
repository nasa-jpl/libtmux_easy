# pytest plugin (`libtmux_easy.testing`)

A `launch_testing` replacement: a test brings up the processes it needs,
talks to them, and **a process that dies fails the test**. Every process is
a shell command. Demos: [demos/pytest](../demos/pytest). ROS 2 lives in a
separate project, `lte_launch_test`, built on this.

```python
# conftest.py
pytest_plugins = ["libtmux_easy.testing"]

# test_system.py
from libtmux_easy.testing import check_health, new_processes

class TestSystem:
    FAIL_ON = {"*": r"Traceback"}             # class attrs, else the module's

    @classmethod
    def setup_class(cls):
        cls.procs = new_processes(cls)        # empty; logs in a tmp dir
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
```

## Options

| option | effect |
|---|---|
| (none) | processes are subprocesses; their output in the terminal, prefixed `[window:name]` |
| `--tmux` | in a terminal, pytest reruns itself under `lte-session` and attaches: the dashboard, test output in the home pane |
| `--tmux --no-attach` | a detached tmux session `lte_test_<pid>`; `tmux attach -t ...` is printed |
| `--keep-open` | as `--tmux`, and nothing is ever stopped: inspect after the run |
| `--arg NAME=VALUE` | read with `arg(NAME, default)`, typed like the default; repeatable |
| `--lte-mode shell\|tmux\|dash` | pick the backend directly (no re-exec) |
| `--lte-keep failure\|always\|never` | detached tmux: when to keep the session (default: on failure) |
| `--lte-close-windows` | `tmux_session` under lte-session: close each test's windows |

## Declaring processes

| style | scope | starts | stopped |
|---|---|---|---|
| `new_processes(cls)` in `setup_class` (preferred) | class | `start()` calls | `close()` in `teardown_class`; if setup fails, when the class ends |
| `new_processes(module)` in `setup_module` | module | same | `close()` in `teardown_module` |
| `PROCESSES = {...}` + `processes` fixture | module | the dict, then waits for `READY` | after the module's last test |
| `launcher` fixture | test | `start()` calls | `stop(name)`, or after the test |
| `tmux_session` fixture | test | a raw `TmuxSession` (tmux only) | after the test |

All but `tmux_session` are `Processes` objects sharing the mode, the run's
tmux session, the health checks and the failure handling. Names are
`window:pane`; a name without `:` gets a window of its own. tmux windows are
`<module>.<Class>/<window>`, `<module>/<window>` or `<module>.<test>/<window>`.

`@parametrize("rate", [5, 10])` on a class runs it all, `setup_class` to
`teardown_class`, once per value: `TestX[rate=5]`, `TestX[rate=10]`, the
value as `cls.rate`. Several names (`"a, b"`, tuples) or stacked
decorators multiply.

## `Processes`

| method | does |
|---|---|
| `start(name, cmd, ready=, timeout=, fail_on=, exits=, stop_codes=)` | start; `ready`: wait for a matching line. `exits`: codes it may finish with on its own (no death). `stop_codes`: codes allowed when stopped (default 0) |
| `stop(name, sigint_timeout=, check=True)` | SIGINT, then SIGTERM, SIGKILL; fails unless it stopped cleanly |
| `wait_for(name, regex, timeout)` | a line matching (`re.Match`); name `"*"`: any process |
| `wait_for_in_order(name, [regex, ...], timeout)` | lines in that order, each later than the last |
| `wait_for_exit(name, timeout)` | an `exits=` process finishing; fails on a wrong code |
| `wait_until(fn, timeout)` | `fn()` until truthy, checking health between calls |
| `output(name)`, `log(name)`, `status(name)` | what it printed (latest start), its log file, its `PaneStatus` |
| `check()`, `problems()` | raise / list deaths and `FAIL_ON` hits not reported yet |
| `close()`, `abort()`, `shutdown()` | stop with checks / stop now without / stop and list what was unclean |

Module functions: `new_processes(owner)`, `check_health()` (every open
group), `arg(name, default)`, `parametrize(names, values)`.

Class or module attributes (the class's win):

| attribute | default | meaning |
|---|---|---|
| `FAIL_ON` | none | `{name or "*": regex, function line -> problem or None, or a list}`: a match is a death |
| `EXITS` | none | `{name: code or codes}`: as `start(exits=)` |
| `STOP_CODES` | 0 | `{name or "*": codes}`; a signal N is 128+N, so 130 allows dying of SIGINT |
| `READY_TIMEOUT` | 30 | seconds, for `ready` / `READY` |
| `SIGINT_TIMEOUT` | 10 | seconds SIGINT gets before SIGTERM |
| `abort()` | none | a classmethod (or module function) the plugin calls when a test fails |

## Health and failure

| when | what | outcome |
|---|---|---|
| a process exits, not stopped and not an expected `exits=` code | recorded (exit watch) | a death |
| a `FAIL_ON` match; an `lte_output_checks` problem | recorded (output watch) | a death |
| in the test: `check()`, `check_health()`, `wait_until`, `wait_for*` | checked | FAILED now |
| after each test | `pytest_runtest_call` checks every open group | FAILED, with the last output and the log's path |
| a test (or `setup_class`) fails | `pytest_runtest_makereport`: the owner's `abort()`, then `Processes.abort()` on the class's, module's and test's groups | stopped at once, no checks; the rest of the class or module SKIPPED |
| `teardown_class`: `close()` | SIGINT, escalating, in parallel | ERROR unless each stopped with a `stop_codes` code; nothing to do after an abort |

Each problem is reported once, by the first check after it happened.
`--keep-open` turns failure handling off: nothing is stopped.

## Logs

Each process's output, as plain text, goes to `<window>.<name>.log`: in
`<basetemp>/<module>.<Class>N/` (`new_processes`), `<basetemp>/<module>N/`
(`processes`) or the test's `tmp_path` (`launcher`). The summary prints
`process logs: <basetemp>`; a death's report names the file. Each process
gets `LTE_LOG_DIR`, its log's directory (e.g. for a tool's own logs).

## Hooks for the app

| fixture | scope | default | use |
|---|---|---|---|
| `lte_pane_setup` | session | `None` | shell command before each process (`cd`, `export`) |
| `lte_output_checks` | session | `[]` | `(name, output) -> [problems]` over whole outputs |

Processes get pytest's environment in every mode (tmux panes via
`env=os.environ`: the server's may predate a sourced workspace).

## Design

- **Backends.** `_ShellBackend`: `Popen(["bash", "-c", cmd])` per process, a
  reader thread (plain text, log, `[name]` prefix, `FAIL_ON`) and a waiter
  thread (exit). `_TmuxBackend`: wrap-mode panes (`lte-run --tee <log>`),
  exit and output watches from `TmuxSession`. `Processes` holds what is
  common: names, `_Run` per start (stopping, died, finished), deaths,
  `FAIL_ON` hits, report-once counters.
- **Reading output.** Shell: the reader's lines. tmux: the `--tee` log, not
  `capture-pane`, which also holds the shell's prompt and the typed command.
  `--tee` rewrites the log on each start, so it holds the latest start.
- **One registry.** Every open `Processes` is in `_open`; `check_health()`
  and the hooks walk it, so helpers deep in test code need no handle.
  `new_processes()` works outside fixtures through `_plugin`, filled by a
  session autouse fixture before any `setup_class`.
- **Ownership.** A group's `owner` (class, module, or the test item for
  `launcher`) decides which failures abort it and which tests are skipped
  after. A `processes`-fixture group skips only tests that use it.
- **`--tmux` attach.** `pytest_cmdline_main` (tryfirst) execs
  `python -m libtmux_easy.launch -- python -m pytest <same args>`. pytest
  already captures fds 0-2 there: the tty check uses `/dev/tty` and the
  foreground process group, and capture (stdin included) is suspended
  before the exec so the new process gets the real terminal.
- **`parametrize`.** A `pytest_pycollect_makeitem` hook turns a marked class
  into one `pytest.Class` node per value, each over a subclass with the
  values as attributes (`_ParametrizedClass._getobj`: pytest 9 looks a
  class up in its module by name otherwise).
- **Concurrency.** Sessions are `lte_test_<pid>` (`-N` if taken, never
  killed); window prefixes get `#N` if taken. Ports and fixed paths are the
  test's job (derive them from `os.getpid()`).

## Shell mode: signals (*verified*, ROS 2 Jazzy)

- `ros2 launch` with a non-tty stdin is "noninteractive" and re-sends SIGINT
  to children that already got the group's. Python nodes die (-2) of the
  second. `ros2 run` doesn't forward SIGINT at all.
- So each process runs with `start_new_session` and a **pty for stdin**, and
  is stopped with `killpg`: SIGINT, then SIGTERM (5 s), then SIGKILL (2 s).
- `ros2 launch` exits 0 on SIGINT and keeps running after a child dies; the
  only record is `[ERROR] [name-N]: process has died [pid P, exit code C`.
  Hence `FAIL_ON` functions (`lte_launch_test.launch_death`).
- A bare `rclpy.spin()` dies *by* SIGINT; catch `KeyboardInterrupt` /
  `ExternalShutdownException` to exit 0, or allow 130 in `STOP_CODES`.
