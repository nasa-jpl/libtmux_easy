# Dashboard (`dashboard()`, `lte-dash`)

```
window "dash" (always the first window)
+--------------------+------------------------------------------+
| sidebar (Textual)  | viewport: the selected pane, swapped in   |
| 3 running 1 failed |                                           |
| home               |                                           |
| main               |                                           |
|  * server running  |                                           |
|  x worker exit 3   |                                           |
+--------------------+------------------------------------------+
```

## Select = swap

The viewport is not a mirror: `swap-pane` moves the **real** pane in, so vim,
copy mode, mouse and Ctrl-C all work. The sidebar is only a list; tmux
renders everything else.

| alternative | why not |
|---|---|
| poll `capture-pane`, forward `send-keys` | laggy; cursor, mouse, alt-screen apps break |
| terminal-emulator widget running a nested `tmux attach` | slow, nested prefix, unmaintained deps |

- The **home** pane is the placeholder: it fills the viewed pane's slot in
  that pane's window, so every window keeps its layout. At most one pane is
  away from home.
- Pane ids survive swaps, so tracking, titles and status are unaffected.
- `repair()` recovers when the viewed pane exits (its slot goes with it) or
  home is killed, and restores the last intact layout.
- `open_window()` creates the window with `new-window -b -d -t {session}:^`, so
  the dashboard is always first.

## Data and actions

| direction | channel |
|---|---|
| status in | the status file (`@lte_status`), polled; entries merged across files under `lte-session` |
| live order, titles | `list-panes -s -F` |
| actions out (`x`, `r`, `X`, `Q`) | `{"cmd": "stop" \| "respawn" \| "stop_all" \| "shutdown", "pane": ...}` on `ctl.sock` |

- The script runs actions on a thread of their own (`stop_pane` blocks).
- All dashboard state is in tmux options ([architecture.md](architecture.md)),
  so a restarted sidebar and the library agree.
- The summary line counts running, stopped, failed (plus ok and unknown when
  nonzero).
- **Home** shows the script's stdout, stderr and logging via `tee_home()`
  (`dash/homelog.py`).

**Public**, used by tlaunch: `lte-dash --open`, `dash.open_window()`, the
status-file fields, the control commands. A non-library launcher keeps the
status file current and answers the socket.

## Borrowed keys (`dash/keys.py`, `lte-key`)

`prefix g` (back to the dashboard) must not need `~/.tmux.conf`, and must not
clobber the user's binding:

- bound conditionally: where session option `@lte_dash_window` is set, go to
  the dashboard; elsewhere, run the original binding;
- returned on exit, including SIGHUP and SIGTERM;
- reference-counted, so two dashboards (or tlaunch's `prefix v`) share.

## Packaging

Textual is optional (`[dash]` extra). `dash.launcher()` picks this
interpreter if it has Textual, else an installed `lte-dash`.

## Ideas

Per-pane log in `less` (`App.suspend()`), filter, "failed only" view, run
and exit times (needs timestamps in `PaneStatus`).
