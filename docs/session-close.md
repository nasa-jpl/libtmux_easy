# Stopping processes and closing a session

## Which to use

| you want | from the script | dashboard | shell / tmux |
|---|---|---|---|
| stop one pane | `stop_pane(pane)` | `x` | Ctrl-C typed in the pane (only SIGINT, no escalation) |
| stop every pane | `stop_all()` | `X` `X` | |
| rerun a pane | `respawn_pane(pane)` | `r` | |
| close gracefully | `close()` | `Q` `Q` | `lte-close`; `prefix C-x` once bound (below) |
| kill now | `kill_session()` | | `tmux kill-session`, closing the last window |
| let go, leave it up | `release(keep_windows=True)` | `q` closes the sidebar only | |

- "Stop" is SIGINT, then SIGTERM, then SIGKILL (10 / 5 / 2 s), stopping at
  the first that works. How each pane mode delivers them:
  [exit-status.md](exit-status.md#stopping).
- "Kill" sends only SIGHUP: no graceful shutdown, no close actions. tmux has
  no hook *before* a session closes. `session-closed` fires after it's
  destroyed, by which time every pane has had SIGHUP (*verified*, 3.4).
  `kill-session` can't be intercepted either. So a graceful close has to run
  before tmux closes anything: that's `lte-close`.

Automatic stops:

| who | when | does |
|---|---|---|
| `lte-session` | the command ends | closes the session (switches you back, or detaches), unless `--keep` |
| `lte-session` | Ctrl-C in its terminal while following | Ctrl-C to the command; a second kills the session |
| `lte-session` | SIGTERM | kills the session |
| pytest plugin | teardown | `stop_all()`, then kill or keep ([pytest-plugin.md](pytest-plugin.md)) |

## `lte-close`

```
lte-close [-t SESSION] [--timeouts INT,TERM,KILL]   # close (default: this session)
lte-close add [-t SESSION] [--before] -- COMMAND     # add a shell close action
lte-close list | clear [-t SESSION]
```

Stdlib only; works on any session, library or not. Linux only (`/proc`).

### Sequence

`lte-close` opens a `close` window (`@lte_close_window`; a second call just
selects it) and runs `lte-close --here` in it:

| step | who | what |
|---|---|---|
| 1 | lte-close | shell actions, `when="before"` |
| 2 | the script, if `ctl.sock` answers | `{"cmd": "close"}`: sets `@lte_closing` `stopping`, Python `before` callbacks, `stop_all()`, `after` callbacks, `done`. lte-close prints status-file changes meanwhile |
| 3 | lte-close | every other pane with something running: `send-keys C-c`, then SIGTERM, SIGKILL to its processes (`--timeouts`) |
| 4 | lte-close | shell actions, `when="after"` |
| 5 | lte-close | `kill-session` (after a 1.5 s pause to read the summary) |

- Ctrl-C in the close window skips to step 5.
- The script must take the command within 2 s, or step 3 does its panes too
  (a script that doesn't know `close`, e.g. tlaunch's).
- Step 3 skips the close pane, the dashboard sidebar and an idle home pane
  (not lte-session's, which runs the command). "Running": a shell pane's
  descendants (the shell stays), else the pane process and its descendants.

### Close actions

| kind | registered with | runs | lives |
|---|---|---|---|
| shell command | `session.on_close("./cleanup.sh")`, `lte-close add [--before] -- CMD` | in the close window, in the cwd it was added from | session option `@lte_on_close` (JSON): survives the script |
| Python `fn(session)` | `session.on_close(fn, when=)` | in the script, on `close` | only while the script runs |

`when`: `"before"` (panes still running) or `"after"` (default). An action
that fails is reported; the close goes on.

### Triggers

| trigger | how |
|---|---|
| a key, from the script | `session.bind_close_key("C-x")`: borrowed like `prefix g` ([dashboard.md](dashboard.md)), so other sessions keep the old binding. Given back on `kill_session()` / `release()`, or once a close starts |
| a key, every session | `~/.tmux.conf`: `bind-key C-x run-shell -b "lte-close -t '#{session_id}'"` |
| the dashboard | `Q` `Q` runs lte-close. With tlaunch's status file (`mode: "tlaunch"`) it still sends `shutdown` |
| the script | `session.close()`: the same steps in-process (shell actions log through `logging`), then `kill_session()` |

## Not done

- A popup instead of a window.
- Per-pane stop commands (e.g. `ros2 lifecycle set ... shutdown`): use a
  `before` action.
