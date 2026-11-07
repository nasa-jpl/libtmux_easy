# Tests

```sh
uv run pytest                  # all, about 4 min
uv run pytest test/output      # one area
```

Each run starts a private tmux server (`conftest.py`): your sessions are
never touched, and two runs don't collide.

| dir | covers |
|---|---|
| `session/` | sessions, windows, panes; exit status in each mode (`test_session.py`); lte-run and wrap mode (`test_wrap_mode.py`); closing and stopping (`test_close.py`); `proc` helpers (`test_proc.py`) |
| `output/` | capture, search, watches, `expect()`, `since=` |
| `dash/` | the dashboard (`test_dash.py`), lte-key (`test_keys.py`), starter tmux settings (`test_starter.py`), lte-session and `TmuxSession.current()` (`test_launch.py`) |
| `processes/` | `processes.Processes` used without the plugin: subprocess and tmux groups, parallel close, `kill_all` |
| `pytest/` | the pytest plugin, each case an inner pytest run in subprocess and tmux mode: `Processes` (`test_plugin.py`), testing session scripts with layout, `build()`, `adopt()` (`test_scripts.py`), the plugin under lte-session (`test_under_lte_session.py`) |

| file | shared |
|---|---|
| `conftest.py` | the private server; `make_session`, `session` fixtures |
| `helpers.py` | `wait_until`, `fmt`, `pane_text`, `title`, `lte_session()`, `SRC` |
| `pytest/conftest.py`, `pytest/inner.py` | `run` (an inner pytest), `MODES`, `PROC` |

Exec-mode tests `xfail` when tmux loses a status (docs/KNOWN_ISSUES.md).
Test file names are unique across dirs (pytest's default import mode).
