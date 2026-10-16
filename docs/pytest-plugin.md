# pytest plugin (`libtmux_easy.testing`)

```python
# conftest.py
pytest_plugins = ["libtmux_easy.testing"]

# test_web.py
PROCESSES = {"servers:web": "python3 -m http.server 8000"}
SIGINT_TIMEOUT = 10                       # optional

def test_serves(processes): ...
```

A `launch_testing` replacement: the module says what to launch, tests talk
to it, and **a process that dies fails the test**.

## Modes (`--lte-mode`)

| mode | processes | terminal | after the run |
|---|---|---|---|
| `shell` | subprocesses (`_ShellBackend`) | output prefixed `[name]` | stopped |
| `tmux` | wrap panes in a detached `lte_test_<pid>` | test output only | closed on success, kept on failure (`--lte-keep failure\|always\|never`) |
| `dash` | wrap panes in lte-session's session | home pane | windows kept while the session is (`lte-session --keep`; `--lte-close-windows` to close) |

`auto` (default): dash under a dashboard session, else tmux if installed,
else shell. Windows are `<module>/<window>`, so modules share a session.
Every mode gives processes pytest's environment: shell mode inherits it,
the tmux modes pass `env=os.environ` (the server's may predate a sourced
workspace).
A kept session is printed in the summary: `tmux attach -t lte_test_<pid>`.

## Health

| when | check | outcome |
|---|---|---|
| a process exits mid-module | `on_exit` records it | |
| after each test | `pytest_runtest_call` hookwrapper | test FAILED with the process's last output |
| any time | `lte_output_checks` callables over output | same; catches deaths only output shows |
| teardown | `shutdown()`: SIGINT, escalating | ERROR unless each exited 0 on SIGINT |

`Processes`: `output(name)`, `problems()`, `check()`, `shutdown(sigint_timeout)`.

## Hooks for the app

| fixture | scope | default | use |
|---|---|---|---|
| `lte_pane_setup` | session | `None` | shell command before each process (source an env, `cd`) |
| `lte_output_checks` | session | `[]` | `(name, output) -> [problems]`, e.g. a supervisor's "process has died" line |
| `tmux_session` | function | | a `TmuxSession` of the test's own |

The ROS demo (`~/projects/lte_launch_test_demo`) adds ROS through these
two hooks only.

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
