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
| `started` for a tracked pane | a new run: reruns typed by hand are tracked |
| `started` from a shell pane | upgrades the pane to wrap mode |
| EOF without `exited` | wrapper died: run is `lost`; a pidfd watch on the target takes over (status unknown) |
| no listener (script gone) | runs unsupervised; same output and status |

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
