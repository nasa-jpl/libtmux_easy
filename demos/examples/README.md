# libtmux_easy examples

Short scripts, one feature each: plain libtmux_easy, no tour scaffolding.
For guided, interactive tours of the same features see `../interactive/`.

| example | shows |
|---|---|
| `basic.py` | two windows of panes, a layout, `mode="wrap"`, `send_keys` by name, then attach |
| `exit_status.py` | the same exits in shell, wrap and exec mode: `exit 130` vs a SIGINT death |
| `process_control.py` | `on_exit`, `stop_pane`'s SIGINT -> SIGTERM escalation, `respawn_pane`, `stop_all` |
| `dashboard.py` | the dashboard over two windows of panes, then attach (needs Textual) |
| `monitoring.py` | another program reading `status_file=`: plain JSON |
| `output.py` | `on_output`, `wait_for_output`, `expect`, a window `capture` and `search_output` |
| `pytest_plugin/` | `test_web.py`: the `processes` fixture (`PROCESSES`, `READY`, `FAIL_ON`); `test_launcher.py`: `launcher`, tests starting and stopping their own |
| `../ros/` | ROS 2: a launch file and nodes under test, an rclpy probe, nodes per test |
| `lte_session/` | tests that start their own panes (`tmux_session`), check their output and exit codes, stop them; under `lte-session` |

```bash
../../.venv/bin/python exit_status.py               # from a checkout; or any Python with libtmux_easy
pytest pytest_plugin                                # --tmux to watch, --keep-open to leave up
lte-session -- pytest -s lte_session                # the dashboard shows each test's panes
```

Run them outside tmux: `basic.py` and `dashboard.py` attach (`prefix d`
detaches; `tmux kill-session -t lte_example_...` when done). The others
print what they see and close their session.
