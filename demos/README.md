# libtmux_easy demos

Runnable scenarios. Each makes its own tmux session, prints checked results
and cleans up. They fall back to `../src`, so no install is needed; only tmux.

## Quick start

```bash
cd demos
./demo_modes.py               # one demo
./demo_modes.py --attach      # ...then attach to look at the panes
./run_all.py                  # all scripted demos, summarized (--only exit)
```

| flag | effect |
|---|---|
| `--attach` | attach to the session when the demo finishes |
| `--keep` | leave the session running |
| `--status-file PATH` | write pane status as JSON on every change |

Exit code 0 only if every check passed, so `run_all.py` is a smoke test.

## The demos

| demo | shows |
|---|---|
| `demo_layout.py` | windows, panes, layouts, naming, `print()` / `print_status()` |
| `demo_modes.py` | shell, exec and wrap mode side by side: exit codes, `exit 130` vs SIGINT, reruns |
| `demo_exit.py` | exit outcomes, `on_exit` / `wait_for_exit`, `stop_pane` escalation, `stop_all` |
| `demo_interference.py` | staying correct while someone types in a pane |
| `demo_launch_like.py` | a supervisor that exits 0 even when a child failed, and how to catch it |
| `demo_monitor.py` | monitoring from outside: titles, `status()`, the status file |
| `demo_dashboard.py` | interactive: the dashboard (needs Textual; `--many` for 36 panes) |
| `pytest_plugin/` | the pytest plugin in shell, tmux and dash mode (see its docstring) |

The last two are not in `run_all.py`. Start with `demo_modes.py`, then
`demo_launch_like.py`.

## Watching live

```bash
./demo_monitor.py --keep &
tmux attach -t lte_demo_monitor       # borders read [running] -> [exited 42]

./demo_monitor.py --status-file /tmp/panes.json --keep &
watch -n1 'python3 -m json.tool /tmp/panes.json'
```

## Workloads

`common.py` has the short ones (`exits_after`, `runs_forever`,
`ignores_sigint`, `graceful_sigint_exit`, ...). They are single-line: in
shell mode a newline would submit the command early.

`workloads/fake_launch.py` behaves like `ros2 launch`: a child dying is
logged and the supervisor keeps running; the child's code appears only in
that line; on SIGINT the supervisor exits 0 regardless.
`python3 workloads/fake_launch.py --crash-after 2` shows it.

## Rough edges

- **Shell mode guesses the pid.** A shell loop has no stable child, and the
  snapshot can catch a `.bashrc` process; either makes a running pane look
  finished. Hence the demos' single long-lived processes, and the occasional
  `demo_launch_like.py` failure. Wrap mode is told the pid.
- **Exec mode can lose the status** (tmux; see `docs/KNOWN_ISSUES.md`). The demos
  print exec results without checking them.
- **`$?` is one-shot**: captured at exit and cached, since anything run in
  the pane later overwrites it (`demo_interference.py`).

Clear strays: `tmux ls | grep '^lte_demo_' | cut -d: -f1 | xargs -r -n1 tmux kill-session -t`
