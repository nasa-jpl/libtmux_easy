# libtmux_easy demos

Runnable scenarios for test-driving the library. Each demo creates its own
tmux session, prints checked results, and cleans up after itself.

No installation needed — the demos fall back to `../src` if `libtmux_easy`
isn't importable. Only `tmux` itself is required.

## Quick start

```bash
cd demos
./demo_pane_modes.py            # run one
./demo_pane_modes.py --attach   # ...then attach to look at the panes
./run_all.py                    # run everything, summarize
./run_all.py --only events      # filter by filename substring
```

Every demo takes:

| flag | effect |
|---|---|
| `--attach` | attach to the session when the demo finishes |
| `--keep` | leave the session running, don't attach |
| `--status-file PATH` | write pane status as JSON on every transition |

Exit code is 0 only if every check passed, so these double as a smoke test.

## The demos

| demo | shows |
|---|---|
| `demo_layout.py` | windows, panes, layouts, naming rules, `print()` / `print_status()` |
| `demo_pane_modes.py` | shell vs exec mode side by side; rerun via `send_keys` vs `respawn_pane` |
| `demo_wrap_mode.py` | all three modes side by side: `exit 130` vs SIGINT, a rerun typed by hand |
| `demo_exit_status.py` | every exit outcome (clean / failing / signal / running) and `stop_pane`'s SIGINT→SIGTERM→SIGKILL escalation |
| `demo_exit_events.py` | `on_exit` callbacks and `wait_for_exit`; why the status is captured at exit rather than read later |
| `demo_interference.py` | staying correct when someone attaches and types in a pane |
| `demo_launch_like.py` | supervising a process that exits 0 even when a child failed — the ROS-launch problem, without ROS |
| `demo_monitor.py` | the three levels of monitoring: pane titles, `status()`, status file |

Start with `demo_pane_modes.py` for the core idea, then `demo_launch_like.py`
for the problem the exit-status work exists to solve.

## Watching a session live

Pane border titles carry each pane's state, so attaching *is* a monitor:

```bash
./demo_monitor.py --keep &
tmux attach -t lte_demo_monitor     # borders read [running] -> [exit 42]
```

For an external monitor with no tmux knowledge:

```bash
./demo_monitor.py --status-file /tmp/panes.json --keep &
watch -n1 'python3 -m json.tool /tmp/panes.json'
```

## Workloads

`workloads/fake_launch.py` stands in for `ros2 launch`. It reproduces the
three behaviors that make a real launch process hard to monitor, all measured
against ros2 jazzy:

1. A child dying is logged, and the supervisor **keeps running**.
2. The child's exit code appears **only** in that log line.
3. On SIGINT the supervisor exits **0** regardless — so its own exit status
   cannot distinguish a healthy run from one where a child crashed.

Run it directly to see the output: `python3 workloads/fake_launch.py --crash-after 2`

The shorter workloads live in `common.py` (`exits_after`, `runs_forever`,
`ignores_sigint`, `graceful_sigint_exit`, ...). They are all single-line,
because in shell mode the command is typed into a shell with `send_keys` and
an embedded newline would submit the line early.

## Known rough edges

Found while writing these demos. Worth knowing before you trust a result.

**Shell loops have no stable process to track** (shell mode only; use wrap
mode, which is told the pid). A command like
`while true; do date; sleep 1; done` is executed by the shell itself, so its
only children are short-lived (`date`, `sleep`). The target-pid snapshot
catches one of those, it exits a second later, and the pane is reported as
finished while the loop is still going. Use a single long-lived process
(`sh -c 'exec vmstat 2'`) instead — that is why the demos avoid shell loops.

**The pid snapshot can race the pane's shell startup** (shell mode only; use
wrap mode). `new_pane` records the
target by waiting for the shell's first child. If the shell is still running
its `.bashrc` when the command is sent, the snapshot can catch a startup
process instead, which then exits and makes the pane look finished. Observed
intermittently in `demo_layout.py` (a pane running `ping` reported three
target pids and not-running); the same command in isolation records one pid
and reports correctly. Waiting for the shell to go idle before sending the
command would close this.

**A fast command may record no target pid at all.** If it finishes before the
first poll, `target_pids` is empty. This is handled — the pane is reported as
finished and `$?` is read — but `PaneStatus.target_pids` will be `()`, so
don't treat an empty tuple as an error.

**Shell mode cannot tell `exit 130` from a SIGINT death.** Both surface as
`128+N`. `ExitStatus.inferred` is `True` whenever the signal was derived that
way. Exec and wrap mode can tell them apart; exec mode has the next entry's
problem, wrap mode doesn't.

**Exec mode can intermittently lose the exit status.** This is a tmux issue,
not a library one. See "Known issues: exec mode" in the main README. It is why
`demo_pane_modes.py` fails roughly 1 run in 8: one of its exec checks reads
`unknown`, and `Pane ... is dead but tmux never recorded its exit status` is
logged. That failure is real. The demo is not broken.

**Reading `$?` is one-shot.** It is captured when the process exits and cached.
Anything else that runs in the pane afterwards overwrites the shell's `$?`, so
a lazy read would be wrong — see the last checks in `demo_exit_events.py` and
`demo_interference.py`.

## Cleaning up

Demos kill their own sessions unless `--keep` or `--attach` is used. To clear
any strays:

```bash
tmux ls | grep '^lte_demo_' | cut -d: -f1 | xargs -r -n1 tmux kill-session -t
```
