# Exit status and exit events

## Pane modes

| mode | status from | interactive | weakness |
|---|---|---|---|
| shell | types `echo MARKER:$?:` after the prompt returns, scrapes it back | yes | `128+N` is ambiguous (`exit 130` vs SIGINT: `inferred`); pid is a guess |
| exec | tmux `#{pane_dead_status}` / `#{pane_dead_signal}` | no | tmux sometimes loses it ([KNOWN_ISSUES](KNOWN_ISSUES.md)) |
| wrap | `lte-run`'s own `waitpid()`, over a socket | yes | one ~12 MB helper per pane |

Wrap is the recommended mode; see [wrap-mode.md](wrap-mode.md).

*Verified*, and why shell mode looks the way it does:

- Exit codes flow child to parent only. tmux learns bash's status, not the
  command's. The command's is in `$?`, readable only as a **separate**
  command: `cmd; echo $?` fails under Ctrl-C, since bash drops the rest of
  the line.
- `$?` is one-shot: the probe or any typed command overwrites it. So the
  status is captured at the exit event and cached.
- `remain-on-exit` must be set **before** the command can exit, or the pane
  (and maybe the server) closes with its status. A split's command starts
  with the pane, so exec mode turns it on for the window during the split,
  then on the pane (the window's own value is put back).
- Every pane is a split, a window's first one too (split off its placeholder,
  then the placeholder is killed), so `shell=` always applies.

## Target pids

Liveness is judged on the pids the command started (`PaneStatus.target_pids`),
not "the pane has children". A user's own command in the same pane neither
hides the target's death nor counts as it running.

| mode | target pid |
|---|---|
| exec | the pane pid |
| wrap | reported by `lte-run` |
| shell | the shell's first children, polled up to `target_pid_timeout` (a guess) |

## Exit events

`_PaneWatcher` (one thread per session) waits on `os.pidfd_open()` fds.
*Verified*: works on non-child pids (Linux 5.3+), fires immediately, immune
to pid reuse. Falls back to 0.5 s psutil polling.

On exit, in order:

1. capture the status (while still readable);
2. refresh the pane title;
3. rewrite the status file;
4. run `on_exit` callbacks, each isolated (one raising doesn't stop others).

A callback registered after its run's exit event (`watch_pane()`,
`wait_for_exit()` on a pane that already ended) fires at once. The check and
the event share `_status_lock`, so it fires exactly once; `_ended` holds the
panes whose run has ended, cleared by a new run. All such rules:
[races.md](races.md).

API: `new_pane(..., on_exit=)`, `watch_pane()`, `unwatch_pane()`,
`wait_for_exit(timeout)`.

## Stopping

`stop_pane()` escalates SIGINT -> SIGTERM -> SIGKILL (timeouts 10 / 5 / 2 s),
stopping at the first that works:

| step | shell | exec | wrap |
|---|---|---|---|
| SIGINT | `send-keys C-c`: the foreground group, like a typed Ctrl-C | same | `lte-run` signals the target group |
| SIGTERM, SIGKILL | the shell's descendants (the shell must survive) | descendants plus the pane process | `lte-run` signals the target group; else the recorded targets, never the wrapper |

Escalating past SIGINT means no graceful cleanup: `PaneStatus.stopped` records
the last signal sent. `stop_all()` stops every running pane in parallel.
`respawn_pane()` reruns in place, in any mode. Closing a whole session, and
which stop to use when: [session-close.md](session-close.md).

## Status surfaces

| surface | form |
|---|---|
| pane border title | `server [running]`, `[exited 3]`, `[stopped, SIGTERM]` |
| `status()` / `print_status()` | list of `PaneStatus` / a table |
| status file (`status_file=`) | JSON list of `PaneStatus`, replaced atomically on every transition |

`PaneStatus.state`: starting (`wait_start=False`, see wrap-mode.md), running,
stopped, ok, failed, unknown. A Ctrl-C typed by
hand counts as failed: nothing can tell intent. `ExitStatus.known` is false
while running or when the status was lost; check it before `.code` / `.ok`.
