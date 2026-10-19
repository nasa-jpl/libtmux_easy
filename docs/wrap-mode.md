# Wrap mode (`lte-run`)

```
bash (pane) -> lte-run (job leader) -> target (same process group)
```

`lte-run` starts the target, reports its pid, `waitpid()`s it and reports the
exact status. The pane stays an ordinary interactive shell.

## Signals

| signal | from | lte-run |
|---|---|---|
| SIGINT, SIGQUIT | terminal, to the whole group | ignores it (it must live to report); the child restores the inherited disposition |
| SIGTSTP, SIGCONT | terminal, `fg` | default: job control works |
| SIGTERM, SIGHUP | aimed at the wrapper | forwards to the target group |
| `{"cmd": "signal"}` | the library | delivers to the target group |

"Target group": the target plus the wrapper's descendants in its group, never
the wrapper itself. Don't move the target into its own group: that breaks
Ctrl-Z.

## Protocol (public)

ros2_launch's `tlaunch` speaks it. Bump `PROTOCOL_VERSION` on any change.

Newline-delimited JSON over `<@lte_dir or $LTE_DIR>/ctl.sock`, one
connection per run:

```json
{"event": "started", "version": 1, "pane": "%3", "wrapper_pid": 101, "target_pid": 102, "argv": [...], "time": ...}
{"event": "exited", "target_pid": 102, "code": 42, "signal": null, "time": ...}
{"cmd": "signal", "signal": 2}
```

| case | handling |
|---|---|
| `started` for a tracked pane | a new run: reruns typed by hand are tracked; its `argv` becomes the pane's command (status, `respawn_pane()`); `bash -c X` is shown as X |
| `started` from a shell pane | upgrades the pane to wrap mode |
| EOF without `exited` | wrapper died: run is `lost`; a pidfd watch on the target takes over (status unknown) |
| no listener (script gone) | runs unsupervised; same output and status |

## Tee (`--tee FILE`, `$LTE_RUN_TEE`)

Copies the command's output to FILE, like `tee`, without taking its terminal.

| where | how |
|---|---|
| tmux pane | `pipe-pane -O` into a FIFO a thread reads; the command keeps its tty |
| pane with a library capture (`capture()`, a watch) | the same: the run takes over the pane's one pipe |
| pane piped by someone else (lte-session's home), or no tmux | the command's stdout and stderr go to a pipe, echoed to ours; not a tty |

FILE is published in pane options, chained into the `pipe-pane` call, so
the library's watches and `search_output()` follow it
([output-match.md](output-match.md)):

| option | value |
|---|---|
| `@lte_pipe_file` | FILE, absolute (not set for a FIFO) |
| `@lte_pipe_from` | FILE's size at the start: `-a` runs match only what is new |
| `@lte_pipe_id` | `lte-run-<pid>`: a rerun overwriting FILE with the same size is still new |
| `@lte_pipe_tee` | `1` while the run owns the pipe; unset at the end |

- Set up **before** the command starts: no first line is missed (tlaunch-pane
  needed a gate FIFO for this before).
- On exit: drain until the copy is quiet for 0.15 s (at most 2 s), close the
  pipe-pane (one tmux call, with unsetting `@lte_pipe_tee`), **then** send
  `exited`. Whoever acts on the exit reads a whole file. The pane's earlier
  capture is not restored.
- Plain text by default: escapes stripped, CRLF to LF, a `\r`-redrawn line
  kept as its last state. `--raw` keeps the bytes.
- FILE may be a FIFO (tlaunch-pane's): opened non-blocking, so a FIFO with no
  reader is an error, not a hang. Any open error: the command runs untee'd.
- `$LTE_RUN_TEE` is removed from the command's environment, so an lte-run
  inside it doesn't tee again.
- One pipe-pane per pane: a library capture is taken over; a foreign pipe
  is left alone (the pipe fallback above).
- tmux runs the pipe command through formats and strftime: `#` and `%` in
  the FIFO path are escaped.
- tmux calls: 2 at the start (check the pipe; pipe plus publish), 1 at the end.

## Details that matter

- The library sets the pane's mode **before** typing the command, or a fast
  `started` arrives for an untracked pane and is ignored.
- Commands with shell operators are launched as `bash -c '...'`. Typed by
  hand, `lte-run a && b` supervises only `a`.
- `enter=False` panes wait in `_wrap_pending` and are never probed for `$?`.
- The socket dir falls back to `/tmp` past AF_UNIX's 107-byte path limit.
- `lte-run` is typed bare: it must be on each pane's PATH.
- Under `lte-session`, `wrap.main(argv, channel=_ExitFile)` writes the status
  to a file instead of the socket.

## Not done

- Orphan reaping (`PR_SET_CHILD_SUBREAPER`) for a target that dies hard and
  leaves children.
- A durable per-pane state file, so a new script can re-adopt a session.
- Wrap as the default mode.
