# Design docs

How libtmux_easy works, and why. User-facing docs: the top-level `README.md`.
Facts marked *verified* were measured (tmux 3.3a/3.4, libtmux 0.21 to 0.62,
Linux 6.x); don't re-research them.

| doc | covers |
|---|---|
| [architecture.md](architecture.md) | modules, sessions (created vs adopted), tmux access rules |
| [exit-status.md](exit-status.md) | pane modes, target pids, exit events, stopping, status surfaces |
| [wrap-mode.md](wrap-mode.md) | `lte-run`: signals and the wrap protocol (public) |
| [dashboard.md](dashboard.md) | `lte-dash`: swap-pane viewport, state in tmux options, borrowed keys |
| [lte-session.md](lte-session.md) | running a command as the dashboard's home pane |
| [output-match.md](output-match.md) | pane output: `capture()` to files, `search_output()`, `watch_output()`, `expect()`, `READY` / `FAIL_ON` |
| [session-close.md](session-close.md) | every way to stop panes and close a session; `lte-close`, close actions, the close key |
| [pytest-plugin.md](pytest-plugin.md) | `libtmux_easy.testing`: modes, health checks, hooks |
| [KNOWN_ISSUES.md](KNOWN_ISSUES.md) | tmux bugs, limits, open bugs |

## Why it exists

Built as a test-launch backend to replace ROS `launch_testing`, whose output
drowns test output and whose exit codes miss node death. Requirements:

1. exact exit status for every process;
2. an event the moment one exits;
3. processes stay interactive (attach, type, Ctrl-C, pdb);
4. see everything at once (dashboard);
5. pytest on top.

The library itself stays generic: nothing ROS-specific outside the demos.

To see the features run, take the tours in `demos/interactive/` (start with
`demos/interactive/01_session.py`).
