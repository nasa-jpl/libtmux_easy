# libtmux_easy examples

Short scripts, one feature each: plain libtmux_easy, no tour scaffolding.
For guided, interactive tours of the same features see `../interactive/`;
for the pytest plugin, `../pytest/`.

| example | shows |
|---|---|
| `basic.py` | two windows of panes, a layout, `mode="wrap"`, `send_keys` by name, then attach |
| `exit_status.py` | the same exits in shell, wrap and exec mode: `exit 130` vs a SIGINT death |
| `process_control.py` | `on_exit`, `stop_pane`'s SIGINT -> SIGTERM escalation, `respawn_pane`, `stop_all` |
| `dashboard.py` | the dashboard over two windows of panes, one with a subprocess tree (`p`), then attach (needs Textual) |
| `monitoring.py` | another program reading `status_file=`: plain JSON |
| `output.py` | `on_output`, `wait_for_output`, `expect`, a window `capture` and `search_output` |

```bash
../../.venv/bin/python exit_status.py               # from a checkout; or any Python with libtmux_easy
```

Run them outside tmux: `basic.py` and `dashboard.py` attach (`prefix d`
detaches; `tmux kill-session -t lte_example_...` when done). The others
print what they see and close their session.
