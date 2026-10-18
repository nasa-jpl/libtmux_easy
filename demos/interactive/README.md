# libtmux_easy tours

Hands-on tours of the library. Each one builds a tmux session, narrates what
it does, and waits for Enter before anything happens, so you can watch it
and try things yourself. No install needed (they fall back to `../src`);
tmux is required, and Textual for the dashboard.

## Quick start

```bash
cd demos/interactive
./01_session.py                         # start here
../.venv/bin/python 02_exit_status.py   # tours 2-5: a Python with Textual gets the dashboard
```

| tour | shows |
|---|---|
| `01_session.py` | building a session from Python: windows, panes, layouts, names, `send_keys`, `print_status`; then attaches |
| `02_exit_status.py` | shell, exec and wrap mode; exit codes vs signals; `respawn_pane`; typing `lte-run` yourself |
| `03_process_control.py` | `on_exit` events, a Ctrl-C you type, `stop_pane` escalation, `stop_all`, `respawn_pane` |
| `04_dashboard.py` | the sidebar: browse, focus, `x` / `r` actions, `dashboard_pane`, many panes |
| `05_monitoring.py` | the status file read by another program; a supervisor whose exit status hides failures |
| `pytest_plugin/` | the pytest plugin in shell, tmux and dash mode (see its docstring) |

## How a tour runs

Tours 2-5 relaunch themselves under `lte-session`: the script runs in the
dashboard's **home** pane, and the panes it starts appear in the sidebar.

| to | do |
|---|---|
| browse panes | `Up` / `Down` in the sidebar |
| type into a pane | select it, `Enter` |
| answer the tour | select `home`, `Enter`, then `Enter` |
| leave early | `Ctrl-C` in home, or `prefix d` |

At the end the tour waits for a last Enter, then stops its panes and closes
the session. Without Textual, or with `--no-dash`, you get the home pane
only; switch windows with `prefix n` / `prefix p`.

## Flags

| flag | |
|---|---|
| `--auto` | no prompts, no dashboard: run straight through |
| `--no-dash` | home pane only |

`./run_all.py` runs every tour with `--auto`, as a smoke test.

## Workloads

| file | |
|---|---|
| `common.py` | `Tour` (session, `say`, `pause`), and one-line workloads: `exits_after`, `runs_forever`, `ignores_sigint`, ... |
| `workloads/fake_launch.py` | behaves like `ros2 launch`: logs a child's death, keeps running, exits 0 on SIGINT |
| `workloads/status_monitor.py` | prints a status file as a table every second |

Clear strays: `tmux ls | grep '^lte_demo_' | cut -d: -f1 | xargs -r -n1 tmux kill-session -t`
