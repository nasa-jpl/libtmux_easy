# pytest plugin (`libtmux_easy.testing`)

```python
# conftest.py
pytest_plugins = ["libtmux_easy.testing"]

# test_web.py
PROCESSES = {"servers:web": "python3 -m http.server 8000"}
SIGINT_TIMEOUT = 10                       # optional
READY = {"servers:web": r"Serving HTTP"}  # optional; READY_TIMEOUT = 30
FAIL_ON = {"*": [r"Traceback"]}           # optional; "*" is every process

def test_serves(processes): ...

def test_restart(launcher):                   # a test's own processes
    launcher.start("clients:c", "./client", ready=r"connected")
    launcher.stop("clients:c")                # fails unless exit 0 on SIGINT
    launcher.start("clients:c", "./client")   # restart: fresh output
```

A `launch_testing` replacement: the module says what to launch, tests talk
to it, and **a process that dies fails the test**.

## Fixtures

| fixture | scope | processes | stopped |
|---|---|---|---|
| `processes` | module | `PROCESSES`, waits for `READY` | after the module's last test |
| `launcher` | function | `start(name, cmd, ready=, timeout=, fail_on=)` | `stop(name)`, or after the test |
| `tmux_session` | function | a raw `TmuxSession` (tmux only) | after the test |

Both `processes` and `launcher` are `Processes` objects. They share the
mode, the run's tmux session, `FAIL_ON`, the hooks and the health checks,
and a test can use both. `launcher` windows are `<module>.<test>/<window>`.
A name without `:` gets a window of its own. On restart, shell mode
replaces the subprocess and tmux opens a new pane `<name>~N` beside the
old one, so the old output stays visible.

## Modes (`--lte-mode`)

| mode | processes | terminal | after the run |
|---|---|---|---|
| `shell` | subprocesses (`_ShellBackend`) | output prefixed `[name]` | stopped |
| `tmux` | wrap panes in a detached `lte_test_<pid>` | test output only | closed on success, kept on failure (`--lte-keep failure\|always\|never`) |
| `dash` | wrap panes in lte-session's session | home pane | windows kept while the session is (`lte-session --keep`; `--lte-close-windows` to close) |

`auto` (default): dash under a dashboard session, else shell. `--tmux`:
dash under a dashboard session, else tmux. Windows are `<module>/<window>`,
so modules share a session.
Every mode gives processes pytest's environment: shell mode inherits it,
the tmux modes pass `env=os.environ` (the server's may predate a sourced
workspace).
A kept session is printed in the summary: `tmux attach -t lte_test_<pid>`.

Logs: each process's output, as plain text, also goes to
`<window>.<name>.log` in `<basetemp>/<module>N/` (`processes`) or the
test's `tmp_path` (`launcher`). Shell mode writes it, the tmux modes use
`lte-run --tee`. The summary prints `process logs: <basetemp>`, and a
death's report names the file. `Processes.log(name)` is the path.

Two runs at once: sessions are `lte_test_<pid>`, or `-N` if that exists
(never killed). Window prefixes get `#N` if taken. Ports and fixed paths
are the test's job: derive them from `os.getpid()`.

`--keep-open` (implies `--tmux`; an error with `--lte-mode=shell`): no
process is stopped, at teardown or on a failed `READY`, and the session
(or lte-session's windows) stays up. No teardown shutdown check. Modules
keep their processes as they finish, so two modules on one port clash.

## Health

| when | check | outcome |
|---|---|---|
| setup | each `READY` line, within `READY_TIMEOUT` | ERROR with the last output |
| a process exits mid-module | `on_exit` records it | |
| a process prints a `FAIL_ON` line | output watch records it | as a death |
| after each test | `pytest_runtest_call` hookwrapper | test FAILED with the process's last output |
| any time | `lte_output_checks` callables over output | same; catches deaths only output shows |
| `stop(name)` | SIGINT, escalating | FAILED unless it exited 0 on SIGINT; not a death |
| teardown | `shutdown()`: SIGINT, escalating | ERROR unless each exited 0 on SIGINT; deaths and stops are reported once |

`Processes`: `start()`, `stop()`, `status(name)`, `output(name)`, `log(name)`,
`wait_for(name, pattern, timeout)` (`re.Match`), `problems()`, `check()`,
`shutdown(sigint_timeout)`. Matching:
[output-match.md](output-match.md).

## Hooks for the app

| fixture | scope | default | use |
|---|---|---|---|
| `lte_pane_setup` | session | `None` | shell command before each process (source an env, `cd`) |
| `lte_output_checks` | session | `[]` | `(name, output) -> [problems]`, e.g. a supervisor's "process has died" line |
The ROS demo ([demos/ros](../demos/ros)) adds ROS through these two hooks
and a `probe` fixture.

## Shell mode: signals (*verified*, ROS 2 Jazzy)

- `ros2 launch` with a non-tty stdin is "noninteractive" and re-sends SIGINT
  to children that already got the group's. Python nodes die (-2) of the
  second. `ros2 run` doesn't forward SIGINT at all.
- So each process runs with `start_new_session` and a **pty for stdin**, and
  is stopped with `killpg`: SIGINT, then SIGTERM (5 s), then SIGKILL (2 s).
- `ros2 launch` exits 0 on SIGINT and keeps running after a child dies; the
  only record is `[ERROR] [name-N]: process has died [pid P, exit code C`.
  Hence `lte_output_checks`.
- A bare `rclpy.spin()` dies *by* SIGINT; catch `KeyboardInterrupt` /
  `ExternalShutdownException` to exit 0.
