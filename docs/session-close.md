# Closing a session (`lte-close`)

tmux has no hook *before* a session closes. `session-closed` fires after it's
destroyed, by which time every pane has had SIGHUP (*verified*, 3.4).
`kill-session` can't be intercepted either. So a graceful close is its own
action, run before tmux closes anything: `lte-close`.

## Sequence

`lte-close [-t SESSION]` opens a `close` window (`@lte_close_window`; a
second call just selects it) and runs `lte-close --here` in it:

| step | who | what |
|---|---|---|
| 1 | lte-close | shell actions, `when="before"` |
| 2 | the script, if `ctl.sock` answers | `{"cmd": "close"}`: sets `@lte_closing` `stopping`, Python `before` callbacks, `stop_all()`, `after` callbacks, `done`. lte-close prints status-file changes meanwhile |
| 3 | lte-close | every other pane with something running: `send-keys C-c`, then SIGTERM, SIGKILL to its processes (10 / 5 / 2 s, `--timeouts`) |
| 4 | lte-close | shell actions, `when="after"` |
| 5 | lte-close | `kill-session` (after a 1.5 s pause to read the summary) |

- Ctrl-C in the close window skips to step 5.
- The script must take the command within 2 s, or step 3 does its panes too
  (a script that doesn't know `close`, e.g. tlaunch's).
- Step 3 skips the close pane, the dashboard sidebar and an idle home pane
  (not lte-session's, which runs the command). "Running": a shell pane's
  descendants (the shell stays), else the pane process and its descendants.
  Found with `/proc`, so stdlib only, Linux only.

## Actions

| kind | registered with | runs | lives |
|---|---|---|---|
| shell command | `session.on_close("./cleanup.sh")`, `lte-close add [--before] -- CMD` | in the close window, in the cwd it was added from | session option `@lte_on_close` (JSON): survives the script |
| Python callable `fn(session)` | `session.on_close(fn, when=)` | in the script, on `close` | only while the script runs |

`when`: `"before"` (panes still running) or `"after"` (default).
`lte-close list` / `clear` show and drop the shell actions.

## Triggers

| trigger | how |
|---|---|
| a key, from the script | `session.bind_close_key("C-x")`: borrowed like `prefix g` (`dash/keys.py`), so other sessions keep the old binding. Given back on `kill_session()` / `release()`, or once a close starts |
| a key, from `~/.tmux.conf` | `bind-key C-x run-shell -b "lte-close -t '#{session_id}'"` (any session) |
| the dashboard | `Q` `Q` runs lte-close. With tlaunch's status file (`mode: "tlaunch"`) it still sends `shutdown` |
| the script | `session.close()`: the same steps in-process (shell actions log through `logging`), then `kill_session()` |

## Not done

- A popup instead of a window.
- Per-pane stop commands (e.g. `ros2 lifecycle set ... shutdown`): use a
  `before` action.
